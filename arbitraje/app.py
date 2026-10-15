import asyncio, json, os, sqlite3, time, logging, secrets
from pathlib import Path
from datetime import datetime
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from .engine import Config, Engine, Book, D

log=logging.getLogger('arbitraje')
GAMMA='https://gamma-api.polymarket.com'
CLOB='https://clob.polymarket.com'
WS='wss://ws-subscriptions-clob.polymarket.com/ws/market'

def load_env_file(path=Path('.env')):
    """Load Botkeep's project-level .env without overwriting real env vars."""
    if not path.is_file(): return
    for raw in path.read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key,value=line.split('=',1)
        key=key.removeprefix('export ').strip(); value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in "'\"": value=value[1:-1]
        if key: os.environ.setdefault(key,value)

class Store:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT); CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, strategy TEXT, payload TEXT); CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, time REAL, strategy TEXT, payload TEXT);')
    def load(self,name):
        row=self.db.execute('SELECT value FROM state WHERE name=?',(name,)).fetchone()
        return json.loads(row[0]) if row else None
    def save(self,engines):
        with self.db:
            for name,e in engines.items():
                self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(name,json.dumps(e.s)))
                for event in e.events:
                    self.db.execute('INSERT INTO events(time,strategy,payload) VALUES (?,?,?)',(event['time'],name,json.dumps(event)))
                    if event.get('kind')=='transaction':
                        self.db.execute('INSERT INTO transactions(time,strategy,payload) VALUES (?,?,?)',(event['time'],name,json.dumps(event)))
                e.events.clear()
            # Bounded event history; lifetime totals persist in state. No raw-book archive in MVP.
            self.db.execute('DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-20000 FROM events)')
            self.db.execute('DELETE FROM transactions WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM transactions)')
    def recent(self):
        return [dict(strategy=r[0],**json.loads(r[1])) for r in self.db.execute('SELECT strategy,payload FROM events ORDER BY id DESC LIMIT 60')]
    def transactions(self,limit=200):
        limit=max(1,min(int(limit),500))
        rows=self.db.execute('SELECT id,time,strategy,payload FROM transactions ORDER BY id DESC LIMIT ?',(limit,))
        return [dict(json.loads(r[3]),id=r[0],time=r[1],strategy=r[2]) for r in rows]

class Service:
    def __init__(self):
        self.cfg=Config.env()
        self.store=Store(Path(os.getenv('DATA_DIR','data'))/'paper.sqlite')
        self.engines={n:Engine(self.cfg,self.store.load(n)) for n in ['maker_taker','taker_taker']}
        for n,e in self.engines.items(): e.s['strategy']=n
        self.status={'connected':False,'market':None,'error':None,'messages':0,'last_message':None,
                     'fee_rate_assumption':self.cfg.rate,'paper_only':True,'network_verified':False}
        self.books=[Book(),Book()]
        self.market=None; self.end=0; self.ids=[]
        self.seen_trades={}

    async def get(self,session,url):
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def discover(self,session):
        start=int(time.time())//300*300
        slug=f'btc-updown-5m-{start}'
        events=await self.get(session,GAMMA+'/events?slug='+slug)
        if not isinstance(events,list): raise ValueError('Unsupported Gamma response')
        for event in events:
            for m in event.get('markets',[]):
                if m.get('closed') or not m.get('acceptingOrders'): continue
                outcomes=m.get('outcomes',[])
                if isinstance(outcomes,str): outcomes=json.loads(outcomes)
                ids=m.get('positionIds') if m.get('version')=='v2' else m.get('clobTokenIds')
                if isinstance(ids,str): ids=json.loads(ids)
                if not ids or len(ids)!=2 or sorted(outcomes)!=['Down','Up']: continue
                ordered=[str(ids[outcomes.index(x)]) for x in ['Up','Down']]
                return slug,ordered,start+300
        raise ValueError('No supported active BTC 5-minute market')

    async def run(self):
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            while True:
                try:
                    market,ids,end=await self.discover(session)
                    self.market,self.ids,self.end=market,ids,end
                    self.status.update(market=market,error=None)
                    self.books=[Book(),Book()]
                    for i,asset in enumerate(ids):
                        snap=await self.get(session,CLOB+'/book?token_id='+asset)
                        self.books[i].snapshot(snap,time.time())
                    await self.stream(session,ids,end)
                except asyncio.CancelledError: raise
                except Exception as exc:
                    log.warning('Feed error: %s',exc)
                    self.status.update(connected=False,error=str(exc))
                    for e in self.engines.values(): e.s['quote']=None
                    self.store.save(self.engines)
                    await asyncio.sleep(5)

    async def stream(self,session,ids,end):
        async with session.ws_connect(WS,heartbeat=20) as ws:
            await ws.send_json({'assets_ids':ids,'type':'market','custom_feature_enabled':True})
            self.status.update(connected=True,network_verified=True)
            async def heartbeat():
                while True:
                    await asyncio.sleep(10); await ws.send_str('PING')
            hb=asyncio.create_task(heartbeat())
            try:
                while time.time()<end:
                    try: msg=await ws.receive(timeout=1)
                    except asyncio.TimeoutError: continue
                    if msg.type in (WSMsgType.CLOSED,WSMsgType.CLOSE,WSMsgType.ERROR): raise ConnectionError('WebSocket disconnected')
                    if msg.type!=WSMsgType.TEXT or msg.data=='PONG': continue
                    data=json.loads(msg.data)
                    for event in data if isinstance(data,list) else [data]: self.handle(event)
            finally:
                hb.cancel()
                await asyncio.gather(hb,return_exceptions=True)
                self.status['connected']=False
                for e in self.engines.values(): e.s['quote']=None

    def handle(self,msg):
        now=time.time()
        raw_stamp=msg.get('timestamp')
        if raw_stamp:
            try:
                stamp=float(raw_stamp)
                if stamp>1e11: stamp/=1000
                if now-stamp>self.cfg.stale_seconds or stamp-now>5: return
            except (ValueError,TypeError): return
        kind=msg.get('event_type')
        asset=str(msg.get('asset_id',''))
        self.status['messages']+=1; self.status['last_message']=now
        if kind=='book' and asset in self.ids:
            self.books[self.ids.index(asset)].snapshot(msg,now)
        elif kind=='price_change':
            for change in msg.get('price_changes',[]):
                asset=str(change.get('asset_id',''))
                if asset in self.ids: self.books[self.ids.index(asset)].change(change,now)
        elif kind=='tick_size_change' and asset in self.ids:
            self.books[self.ids.index(asset)].tick=D(msg['new_tick_size'])
            for e in self.engines.values():
                if e.s['quote']: e.s['quote']['cancel_at']=now+self.cfg.latency
        elif kind=='last_trade_price' and asset in self.ids:
            key=(asset,msg.get('timestamp'),msg.get('transaction_hash'),msg.get('price'),msg.get('size'),msg.get('side'))
            if key in self.seen_trades: return
            self.seen_trades[key]=now
            if len(self.seen_trades)>5000:
                self.seen_trades={k:v for k,v in self.seen_trades.items() if now-v<60}
            for e in self.engines.values():
                e.tick(self.books,now,self.market,self.end,self.status['connected'])
                args=(self.ids.index(asset),msg['price'],msg.get('size',0),msg.get('side'),now)
                if not e.cancel_fills(*args) and not e.s['pending']: e.trade(*args)
            self.store.save(self.engines)

    async def clock(self):
        last_save=0
        while True:
            now=time.time()
            if self.market:
                for e in self.engines.values(): e.tick(self.books,now,self.market,self.end,self.status['connected'])
            if now-last_save>=1:
                self.store.save(self.engines); last_save=now
            await asyncio.sleep(.1)

    def state(self):
        now=time.time()
        return {'status':self.status,'settings':vars(self.cfg),'strategies':{n:e.s for n,e in self.engines.items()},
                'books':[{'age_seconds':now-b.updated if b.updated else None,
                          'bid':float(max(b.bids)) if b.bids else None,
                          'ask':float(min(b.asks)) if b.asks else None} for b in self.books],
                'events':self.store.recent(),'transactions':self.store.transactions()}

async def main():
    # Botkeep writes configured environment values to a project-level .env file.
    load_env_file()
    service=Service()
    token=os.getenv('DASHBOARD_TOKEN','')
    if len(token)<16: raise ValueError('Set DASHBOARD_TOKEN to at least 16 characters')
    @web.middleware
    async def auth(request,handler):
        if request.path=='/health': return await handler(request)
        supplied=request.headers.get('Authorization','').removeprefix('Bearer ')
        if token and not secrets.compare_digest(supplied,token):
            if request.path=='/': return web.FileResponse(Path(__file__).with_name('panel.html'))
            return web.json_response({'error':'Unauthorized'},status=401)
        return await handler(request)
    app=web.Application(middlewares=[auth])
    async def index(request): return web.FileResponse(Path(__file__).with_name('panel.html'))
    async def state(request): return web.json_response(service.state())
    async def health(request):
        fresh=service.status['connected'] and all(b.fresh(time.time(),service.cfg.stale_seconds) for b in service.books)
        return web.json_response({'alive':True,'feed_ready':fresh,'paper_only':True},status=200 if fresh else 503)
    app.router.add_get('/',index); app.router.add_get('/api/state',state); app.router.add_get('/health',health)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,'0.0.0.0',int(os.getenv('SERVER_PORT',os.getenv('PORT','8080')))).start()
    tasks=[asyncio.create_task(service.run()),asyncio.create_task(service.clock())]
    try: await asyncio.gather(*tasks)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        service.store.save(service.engines); service.store.db.close(); await runner.cleanup()

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

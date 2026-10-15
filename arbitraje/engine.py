from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN
import os

D = lambda v: Decimal(str(v))

@dataclass
class Config:
    capital: float = 1000
    size: float = 10
    edge: float = .01
    buffer: float = .005
    rate: float = .07
    latency: float = .5
    max_unhedged: float = 10
    stop_seconds: float = 60
    stale_seconds: float = 3
    quote_ttl: float = 5
    merge_seconds: float = 2

    @classmethod
    def env(cls):
        c = cls()
        for attr, key in [('capital','PAPER_CAPITAL'),('size','PAIR_SIZE'),('edge','MIN_EDGE'),
                          ('buffer','PRICE_BUFFER'),('rate','FEE_RATE'),('max_unhedged','MAX_UNHEDGED_USD'),
                          ('stop_seconds','STOP_BEFORE_CLOSE')]:
            setattr(c, attr, float(os.getenv(key, getattr(c, attr))))
        c.latency = float(os.getenv('LATENCY_MS','500')) / 1000
        if c.capital <= 0 or c.size <= 0 or c.edge < 0 or c.buffer < 0 or not 0 <= c.rate <= 1 or c.latency < 0 or c.max_unhedged <= 0:
            raise ValueError('Invalid paper configuration')
        return c

class Book:
    def __init__(self):
        self.bids, self.asks = {}, {}
        self.updated = 0
        self.tick, self.minimum = D('.01'), D('5')

    def snapshot(self, msg, now):
        self.bids = {D(x['price']): D(x['size']) for x in msg['bids'] if D(x['size']) > 0}
        self.asks = {D(x['price']): D(x['size']) for x in msg['asks'] if D(x['size']) > 0}
        self.tick = D(msg.get('tick_size', self.tick))
        self.minimum = D(msg.get('min_order_size', self.minimum))
        self.updated = now

    def change(self, msg, now):
        levels = self.bids if msg['side'] == 'BUY' else self.asks
        p, q = D(msg['price']), D(msg['size'])
        if q > 0: levels[p] = q
        else: levels.pop(p, None)
        self.updated = now

    def fresh(self, now, timeout):
        return bool(self.bids and self.asks and now-self.updated <= timeout and max(self.bids) < min(self.asks))

    def cost(self, quantity, buy=True, ceiling=None):
        remain, total, fee = D(quantity), D(0), D(0)
        levels = self.asks if buy else self.bids
        for p in sorted(levels, reverse=not buy):
            if ceiling is not None and buy and p > D(ceiling): break
            q = min(remain, levels[p])
            total += q*p
            fee += q*p*(1-p)
            remain -= q
            if remain <= 0: return float(total), float(fee)
        return None

class Engine:
    def __init__(self, cfg, state=None):
        self.c = cfg
        self.s = state or {'cash':cfg.capital, 'pnl':0., 'quote':None, 'pending':None,
                           'releases':[], 'trades':0, 'failed_hedges':0, 'halted':False}
        # Resting virtual orders cannot be reconstructed safely after downtime.
        self.s['quote'] = None
        self.events = []

    def event(self, kind, now, **fields):
        self.events.append({'time':now,'kind':kind,**fields})

    def exposure(self):
        p = self.s['pending']
        return p['q']*p['maker_price'] if p and p['kind']=='hedge' else 0

    def trade(self, side, price, size, aggressor, now):
        q = self.s['quote']
        if not q or side != q['side'] or aggressor != 'SELL' or now < q['active_at']: return
        # Only prints at our exact price consume queue. Cancellations never improve priority.
        if D(price) != D(q['price']): return
        volume = float(size)
        used = min(q['ahead'], volume)
        q['ahead'] -= used
        fill = min(q['remaining'], volume-used)
        if fill <= 0: return
        self.s['cash'] -= fill*q['price']
        q['remaining'] -= fill
        self.s['pending'] = {'kind':'hedge','side':1-side,'q':fill,'maker_price':q['price'],
                             'due':now+self.c.latency,'market':q['market'],'deadline':q['end']}
        # Stop quoting after the first partial fill; simulate cancel latency.
        q['cancel_at'] = now+self.c.latency
        self.event('maker_fill',now,quantity=fill,price=q['price'])

    def cancel_fills(self, side, price, size, aggressor, now):
        # Further prints while cancellation is in flight must increase inventory.
        q, p = self.s['quote'], self.s['pending']
        if not q or not p or p['kind']!='hedge' or not q.get('cancel_at') or now>=q['cancel_at']: return False
        if side==q['side'] and aggressor=='SELL' and D(price)==D(q['price']):
            fill=min(q['remaining'],float(size))
            q['remaining']-=fill
            p['q']+=fill
            self.s['cash']-=fill*q['price']
            self.event('maker_fill_during_cancel',now,quantity=fill)
        return True

    def tick(self, books, now, market, end, connected=True):
        for item in list(self.s['releases']):
            if now >= item['due']:
                self.s['cash'] += item['amount']; self.s['releases'].remove(item)
        fresh = connected and all(b.fresh(now,self.c.stale_seconds) for b in books)
        q = self.s['quote']
        if q:
            if q.get('cancel_at') and now>=q['cancel_at']: self.s['quote']=None
            elif not q.get('cancel_at') and (not fresh or now>=q['expires'] or now>=end-self.c.stop_seconds):
                q['cancel_at']=now+self.c.latency
        p = self.s['pending']
        if p:
            if p['market'] != market or now>=p['deadline']:
                if p['kind']=='benchmark':
                    self.s['pending']=None
                    return
                self.s['halted']=True
                return  # retain inventory rather than invent a liquidation price
            if now < p['due'] or not fresh: return
            if p['kind']=='hedge':
                cap=1-p['maker_price']  # may accept a loss after fees to remove directional risk
                cost=books[p['side']].cost(p['q'],ceiling=cap)
                if cost and cost[0]+cost[1]*self.c.rate<=self.s['cash']:
                    fee=cost[1]*self.c.rate
                    paid=cost[0]+fee
                    profit=p['q']-p['q']*p['maker_price']-paid
                    self.s['cash']-=paid
                    self.s['releases'].append({'due':now+self.c.merge_seconds,'amount':p['q']})
                    self.complete(now,profit,'paired',p['q'],market=p['market'],execution='maker_taker',
                                  leg1='maker '+('Up' if 1-p['side']==0 else 'Down'),leg1_price=p['maker_price'],
                                  leg2='taker '+('Up' if p['side']==0 else 'Down'),leg2_price=cost[0]/p['q'],fees=fee)
                else:
                    # Unwind maker side at available bids, with a separate execution delay.
                    p['kind']='unwind'; p['side']=1-p['side']; p['due']=now+self.c.latency
                    self.s['failed_hedges']+=1
                    self.event('hedge_failed',now,quantity=p['q'])
            elif p['kind']=='unwind':
                cost=books[p['side']].cost(p['q'],buy=False)
                if cost:
                    fee=cost[1]*self.c.rate
                    proceeds=cost[0]-fee
                    self.s['cash']+=proceeds
                    self.complete(now,proceeds-p['q']*p['maker_price'],'unwind',p['q'],market=p['market'],
                                  execution='maker_unwind',leg1='maker '+('Up' if p['side']==0 else 'Down'),
                                  leg1_price=p['maker_price'],leg2='venta '+('Up' if p['side']==0 else 'Down'),
                                  leg2_price=cost[0]/p['q'],fees=fee)
            elif p['kind']=='benchmark':
                costs=[b.cost(p['q']) for b in books]
                if all(costs):
                    fees=sum(x[1]*self.c.rate for x in costs)
                    paid=sum(x[0] for x in costs)+fees
                    if paid<=self.s['cash'] and p['q']-paid>=p['q']*self.c.edge:
                        self.s['cash']-=paid
                        self.s['releases'].append({'due':now+self.c.merge_seconds,'amount':p['q']})
                        self.s['last_benchmark_market']=market
                        self.complete(now,p['q']-paid,'paired',p['q'],market=p['market'],execution='taker_taker',
                                      leg1='taker Up',leg1_price=costs[0][0]/p['q'],
                                      leg2='taker Down',leg2_price=costs[1][0]/p['q'],fees=fees)
                        return
                self.s['pending']=None
                self.event('benchmark_rejected',now)
            return
        if self.s['halted'] or self.s['quote'] or not fresh or now>=end-self.c.stop_seconds: return
        size=self.c.size
        if size < max(float(b.minimum) for b in books): return
        if self.s.get('strategy')=='taker_taker':
            if self.s.get('last_benchmark_market')==market: return
            costs=[b.cost(size) for b in books]
            if all(costs) and size-sum(x[0]+x[1]*self.c.rate for x in costs)>=size*self.c.edge:
                self.s['pending']={'kind':'benchmark','q':size,'due':now+self.c.latency,'market':market,'deadline':end}
            return
        choices=[]
        for side,b in enumerate(books):
            cost=books[1-side].cost(size)
            if not cost: continue
            limit=1-(cost[0]+cost[1]*self.c.rate)/size-self.c.edge-self.c.buffer
            price=min(max(b.bids),D(limit).quantize(b.tick,rounding=ROUND_DOWN))
            price=(price/b.tick).to_integral_value(rounding=ROUND_DOWN)*b.tick
            if price<=0 or price>=min(b.asks) or size*float(price)>self.c.max_unhedged: continue
            reserve=size*float(price)+cost[0]+cost[1]*self.c.rate+size*self.c.buffer
            if reserve>self.s['cash']: continue
            ahead=float(sum(v for p,v in b.bids.items() if p>=price))
            choices.append((ahead,side,float(price)))
        if choices:
            ahead,side,price=min(choices)
            self.s['quote']={'side':side,'price':price,'remaining':size,'ahead':ahead,
                             'active_at':now+self.c.latency,'expires':now+self.c.quote_ttl,'market':market,'end':end}
            self.event('quote',now,side=side,price=price,queue_ahead=ahead)

    def complete(self, now, profit, kind, quantity, **details):
        self.s['pnl']+=profit; self.s['trades']+=1; self.s['pending']=None
        self.event(kind,now,pnl=profit,quantity=quantity)
        self.event('transaction',now,result=kind,pnl=profit,quantity=quantity,**details)

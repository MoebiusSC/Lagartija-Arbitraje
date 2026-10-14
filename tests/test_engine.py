import unittest, tempfile
from pathlib import Path
from arbitraje.engine import Book,Config,Engine,D
from arbitraje.app import Store

def books(now=100):
    result=[]
    for bid,ask in [('.48','.49'),('.48','.49')]:
        b=Book(); b.snapshot({'bids':[{'price':bid,'size':'2'}], 'asks':[{'price':ask,'size':'100'}]},now); result.append(b)
    return result

class Tests(unittest.TestCase):
    def test_fee_and_depth(self):
        b=books()[0]
        paid,base=b.cost(100)
        self.assertAlmostEqual(paid,49)
        self.assertAlmostEqual(base*.07,1.7493)
        self.assertIsNone(b.cost(101))
    def test_queue_not_touch(self):
        e=Engine(Config()); b=books(); e.tick(b,100,'m',400)
        q=e.s['quote']; self.assertIsNotNone(q)
        e.trade(q['side'],q['price'],1,'SELL',101)
        self.assertIsNone(e.s['pending'])
        e.trade(q['side'],q['price'],3,'SELL',101.1)
        self.assertEqual(e.s['pending']['q'],2)
        self.assertEqual(q['remaining'],8)
    def test_hedge_waits_and_cash_conservation(self):
        e=Engine(Config()); b=books(); e.tick(b,100,'m',400); q=e.s['quote']
        e.trade(q['side'],q['price'],12,'SELL',101)
        e.tick(b,101.1,'m',400); self.assertIsNotNone(e.s['pending'])
        e.tick(b,101.6,'m',400); self.assertIsNone(e.s['pending'])
        self.assertAlmostEqual(e.s['pnl'],.22507)
        b=books(104); e.tick(b,104,'m',400)
        self.assertAlmostEqual(e.s['cash'],1000+e.s['pnl'])
    def test_failed_hedge_and_unwind_loss(self):
        e=Engine(Config()); b=books(); e.tick(b,100,'m',400); q=e.s['quote']
        e.trade(q['side'],q['price'],12,'SELL',101)
        b[1-q['side']].asks={D('.8'):D(100)}
        b[q['side']].bids={D('.46'):D(100)}
        e.tick(b,101.6,'m',400)
        self.assertEqual(e.s['pending']['kind'],'unwind')
        e.tick(b,102.2,'m',400)
        self.assertLess(e.s['pnl'],0)
        self.assertAlmostEqual(e.s['cash'],1000+e.s['pnl'])
    def test_cancel_latency(self):
        e=Engine(Config()); b=books(); e.tick(b,100,'m',400); q=e.s['quote']
        e.trade(q['side'],q['price'],3,'SELL',101)
        e.cancel_fills(q['side'],q['price'],2,'SELL',101.2)
        self.assertEqual(e.s['pending']['q'],3)
    def test_stale_and_expiry(self):
        e=Engine(Config()); e.tick(books(),110,'m',400); self.assertIsNone(e.s['quote'])
        e.tick(books(350),350,'m',400); self.assertIsNone(e.s['quote'])
    def test_unresolved_not_written_off(self):
        e=Engine(Config()); b=books(); e.tick(b,100,'m',400); q=e.s['quote']
        e.trade(q['side'],q['price'],12,'SELL',101)
        e.tick(b,401,'next',700)
        self.assertTrue(e.s['halted']); self.assertIsNotNone(e.s['pending'])
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'paper.sqlite'); e=Engine(Config()); e.s['pnl']=5
            s.save({'maker_taker':e}); new=Engine(Config(),s.load('maker_taker'))
            self.assertEqual(new.s['pnl'],5); s.db.close()
    def test_benchmark_net_fees(self):
        e=Engine(Config()); e.s['strategy']='taker_taker'; b=books()
        e.tick(b,100,'m',400); self.assertIsNone(e.s['pending'])
        for x in b: x.asks={D('.47'):D(100)}; x.bids={D('.46'):D(10)}
        e.tick(b,100,'m',400); self.assertIsNotNone(e.s['pending'])
        e.tick(b,101,'m',400); self.assertGreater(e.s['pnl'],0)

if __name__=='__main__': unittest.main()

import unittest
from broker_connect import paper_moomoo_rows,paper_tiger_rows,masked

class BrokerBoundaryTests(unittest.TestCase):
    def test_moomoo_excludes_real_unknown_and_other_markets(self):
        valid=dict(trd_env='SIMULATE',trdmarket_auth=['US'],sim_acc_type='STOCK_AND_OPTION')
        rows=[valid,{**valid,'trd_env':'REAL'},{**valid,'trdmarket_auth':['HK']},{**valid,'sim_acc_type':'FUTURES'},{}]
        self.assertEqual(paper_moomoo_rows(rows),[valid])
    def test_tiger_requires_explicit_paper_type(self):
        rows=[dict(account='20191221901212121',account_type='STANDARD'),dict(account='a',account_type='PAPER'),dict(account='b')]
        self.assertEqual(paper_tiger_rows(rows),[rows[1]])
    def test_mask(self):
        self.assertEqual(masked('123456789'),'***6789')

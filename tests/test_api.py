import os
import tempfile
import time
import unittest
from pathlib import Path

TEST_DB = Path(tempfile.gettempdir()) / 'kuaiying-test.db'
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ['DATABASE_PATH'] = str(TEST_DB)
os.environ['INTEGRATION_MODE'] = 'mock'

from fastapi.testclient import TestClient
from backend.app import app


class CommerceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()
        cls.headers = {'X-Tenant-ID': 'tenant-qingyang'}

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        if TEST_DB.exists():
            TEST_DB.unlink()

    def test_health_products_and_tenant_isolation(self):
        self.assertEqual(self.client.get('/api/health').json()['integration_mode'], 'mock')
        self.assertEqual(len(self.client.get('/api/products', headers=self.headers).json()), 5)
        self.assertEqual(self.client.get('/api/products', headers={'X-Tenant-ID': 'missing'}).status_code, 404)

    def test_jushuitan_status_reports_missing_official_config(self):
        status = self.client.get('/api/integrations/jushuitan/status', headers=self.headers).json()
        self.assertEqual(status['provider'], 'jushuitan')
        self.assertFalse(status['ready_for_sync'])
        self.assertIn('JST_ACCESS_TOKEN', status['missing_for_sync'])

    def test_sync_is_idempotent(self):
        body = {'idempotency_key': 'unit-test-sync'}
        first = self.client.post('/api/sync/jushuitan', headers=self.headers, json=body)
        second = self.client.post('/api/sync/jushuitan', headers=self.headers, json=body)
        self.assertEqual(first.json()['id'], second.json()['id'])
        job_id = first.json()['id']
        for _ in range(20):
            job = self.client.get(f'/api/sync/jobs/{job_id}', headers=self.headers).json()
            if job['status'] != 'running':
                break
            time.sleep(.05)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['records_updated'], 5)

    def test_drafts_are_explicitly_mocked(self):
        product = self.client.get('/api/products', headers=self.headers).json()[0]
        response = self.client.post('/api/kuaishou/drafts', headers=self.headers, json={'product_ids': [product['id']]})
        self.assertEqual(response.json()['mode'], 'mock')
        self.assertFalse(response.json()['production_synced'])


    def test_crm_login_status_flow_and_dashboard_counts(self):
        login = self.client.post('/api/auth/login', json={'username': 'bd', 'password': 'bd123'})
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': f"Bearer {login.json()['token']}"}
        created = self.client.post('/api/crm/influencers', headers=headers, json={
            'platform': '快手', 'account': 'unit-creator', 'nickname': '单测达人', 'category': '养生茶饮',
            'followers': 1000, 'quote_price': 300, 'contact': 'wx:unit', 'status': '待邀约'
        })
        self.assertEqual(created.status_code, 201)
        influencer_id = created.json()['id']
        self.client.post(f'/api/crm/influencers/{influencer_id}/followups', headers=headers, json={'action': '邀约', 'note': '已发出邀约'})
        self.client.post(f'/api/crm/influencers/{influencer_id}/followups', headers=headers, json={'action': '建联', 'note': '微信已通过'})
        self.client.post(f'/api/crm/influencers/{influencer_id}/samples', headers=headers, json={'sample_date': '2026-06-24', 'sample_name': '试用装'})
        self.client.post(f'/api/crm/influencers/{influencer_id}/deals', headers=headers, json={'deal_date': '2026-06-24', 'amount': 99, 'cooperation_type': '分发'})
        dashboard = self.client.get('/api/crm/dashboard?date=2026-06-24', headers=headers).json()
        self.assertGreaterEqual(dashboard['daily']['sampled'], 1)
        self.assertGreaterEqual(dashboard['daily']['dealed'], 1)
        detail = self.client.get(f'/api/crm/influencers/{influencer_id}', headers=headers).json()
        self.assertEqual(detail['influencer']['status'], '已出单')

    def test_crm_csv_import_validates_required_fields(self):
        login = self.client.post('/api/auth/login', json={'username': 'manager', 'password': 'manager123'}).json()
        headers = {'Authorization': f"Bearer {login['token']}"}
        bad = self.client.post('/api/crm/import-csv', headers=headers, json={'csv_text': 'nickname\n缺字段'})
        self.assertEqual(bad.status_code, 422)
        good = self.client.post('/api/crm/import-csv', headers=headers, json={'csv_text': 'platform,account,nickname\n快手,csv-creator,CSV达人'})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()['created'], 1)


if __name__ == '__main__':
    unittest.main()

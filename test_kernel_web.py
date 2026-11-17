import json,time,unittest
from test_web import WebTests


class KernelWebTests(WebTests):
    # Use existing real HTTP fixture, authentication and CSRF checks. Only the
    # new methods are loaded below; do not duplicate the inherited regression.
    def test_kernel_demo_real_http_and_persistent_summary(self):
        key=json.load(self.request('/api/jobs',{'preset':'kernel','workers':2}))['id']
        end=time.monotonic()+15
        while time.monotonic()<end:
            detail=json.load(self.request('/api/jobs/'+key))
            if detail['status'] not in ('running','cancelling'):break
            time.sleep(.03)
        self.assertEqual(detail['status'],'succeeded',detail.get('error'))
        kernel=detail['kernel'];self.assertEqual(kernel['total_calls'],6)
        self.assertEqual({c['service'] for c in kernel['calls']},{'tool','memory','storage','context','runtime'})
        self.assertNotIn('llm',{c['service'] for c in kernel['calls']})
        self.assertTrue(all(c['pid'] and c['state']=='succeeded' for c in kernel['calls']))
        index=self.request('/console').read().decode()
        self.assertIn('kernelPanel',index);self.assertIn('value="kernel"',index)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite([KernelWebTests('test_kernel_demo_real_http_and_persistent_summary')])

import unittest
from agent_runtime.capabilities import execute_plugin
class AggregateTests(unittest.TestCase):
 def test_exact_decimal_and_rejection(self):
  def run(text):return execute_plugin({'plugin':'csv.aggregate','arguments':{'text':text,'column':'amount'}})
  self.assertEqual(run('amount\n0.1\n0.2')['sum_decimal'],'0.3')
  for text in ['amount\nNaN','amount\n1e999','amount,amount\n1,2','other\n2','amount\n""']:
   with self.subTest(text=text),self.assertRaises(ValueError):run(text)

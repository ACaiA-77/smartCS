import requests

# 测试存在的订单
r = requests.post('http://127.0.0.1:8000/api/tools/call', json={
    'name': 'order_query',
    'arguments': {'order_id': 'ORD-20260801-0100', 'user_id': 'web_user'}
})
print("存在的订单:", r.json())

# 测试不存在的订单
r2 = requests.post('http://127.0.0.1:8000/api/tools/call', json={
    'name': 'order_query',
    'arguments': {'order_id': 'NOT_EXIST', 'user_id': 'web_user'}
})
print("不存在的订单:", r2.json())

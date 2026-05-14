import requests
import datetime

BASE = "http://127.0.0.1:8000"

# Login
r = requests.post(f'{BASE}/auth/login',
    data={'username': 'admin@clinicremind.in', 'password': 'changeme123'})
token = r.json()['access_token']
headers = {'Authorization': f'Bearer {token}'}
print("Login OK")

# Add fresh patient with YOUR real WhatsApp number
r3 = requests.post(f'{BASE}/patients', headers=headers,
    json={'clinic_id': 1, 'name': 'Mounika', 'phone': '917989283001', 'condition': 'test'})
patient_id = r3.json()['id']
print("Patient:", r3.json())

# Add visit with next_visit = tomorrow
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
r4 = requests.post(f'{BASE}/visits', headers=headers,
    json={'patient_id': patient_id, 'visit_date': datetime.date.today().isoformat(), 'next_visit': tomorrow})
print("Visit:", r4.json())

# Trigger reminder
r5 = requests.post(f'{BASE}/test/day-before', headers=headers)
print("Reminder:", r5.json())
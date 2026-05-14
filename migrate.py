import sqlite3
conn = sqlite3.connect('clinic_reminder.db')
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE patients ADD COLUMN age INTEGER')
    print('age added')
except:
    print('age already exists')

try:
    cursor.execute('ALTER TABLE patients ADD COLUMN gender VARCHAR')
    print('gender added')
except:
    print('gender already exists')

try:
    cursor.execute('ALTER TABLE patients ADD COLUMN followup_type VARCHAR')
    print('followup_type added')
except:
    print('followup_type already exists')

try:
    cursor.execute('ALTER TABLE visits ADD COLUMN status VARCHAR DEFAULT upcoming')
    print('status added')
except:
    print('status already exists')

conn.commit()
conn.close()
print('Done!')
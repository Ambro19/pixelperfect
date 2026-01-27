import sqlite3
db = r"C:\Users\xiggy\PIXEL-PERFECT-API\PixelPerfectAPI\backend\pixelperfect.db"
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='screenshots'")
row = cur.fetchone()
print(row[0] if row else "screenshots table not found")
con.close()

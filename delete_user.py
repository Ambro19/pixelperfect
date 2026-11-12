# backend/delete_user.py
from models import SessionLocal, User, DATABASE_URL
from log_safety import mask_db_url

def main(username: str = "SarahSteward"):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        if not u:
            print(f"No such user: {username} (DB={mask_db_url(DATABASE_URL)})")
            return
        db.delete(u)
        db.commit()
        print(f"✅ Deleted user: {username}")
    except Exception as e:
        db.rollback()
        print("❌ Error:", e)
    finally:
        db.close()

if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "SarahSteward")

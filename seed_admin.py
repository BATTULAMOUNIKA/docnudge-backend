from app.database import SessionLocal, engine
from app.models import Base, User
from app.auth import hash_password

Base.metadata.create_all(bind=engine)

db = SessionLocal()

existing = db.query(User).filter(User.email == "admin@clinicremind.in").first()
if existing:
    print("Admin already exists:", existing.email)
else:
    admin = User(
        email="admin@clinicremind.in",
        password=hash_password("changeme123"),
        role="admin",
        clinic_id=None,
    )
    db.add(admin)
    db.commit()
    print("Admin created successfully.")
    print("Email:   admin@clinicremind.in")
    print("Password: changeme123")
    print("Change this password after first login!")

db.close()
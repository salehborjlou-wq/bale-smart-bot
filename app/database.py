from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, Boolean, DateTime
)
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import DATABASE_URL

url = DATABASE_URL
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
engine = create_engine(url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    price = Column(Integer, default=0)
    stock = Column(Integer, default=0)
    active = Column(Boolean, default=True)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), unique=True, nullable=False)
    name = Column(String(200), default="")
    phone = Column(String(50), default="")
    address = Column(Text, default="")
    bot_paused = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), index=True)
    role = Column(String(20))
    text = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), index=True)
    customer_name = Column(String(200))
    phone = Column(String(50))
    address = Column(Text, default="")
    product_name = Column(String(200))
    quantity = Column(Integer, default=1)
    total_price = Column(Integer, default=0)
    status = Column(String(30), default="new")
    created_at = Column(DateTime, default=datetime.utcnow)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id = Column(BigInteger, primary_key=True, autoincrement=False)


def init_db():
    Base.metadata.create_all(engine)

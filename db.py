from pymongo import MongoClient
import os

client = MongoClient(os.getenv("MONGO_URI"))
db = client["subscription_bot"]

channels = db["channels"]
subs = db["subscribers"]
payments = db["payments"]

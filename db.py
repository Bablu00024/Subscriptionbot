from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"))
db = client["subscription_bot"]

channels = db["channels"]
subs = db["subscribers"]
payments = db["payments"]

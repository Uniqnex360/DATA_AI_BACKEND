import requests
import time
import os
import sys
import pandas as pd

# --- CONFIGURATION ---
API_URL = "http://127.0.0.1:8000"
# REPLACE THIS with the actual name of your excel file
YOUR_FILE_NAME = "marine_products.xlsx" 

def run_test():
    # 1. Validation
    if not os.path.exists(YOUR_FILE_NAME):
        print(f"❌ Error: File '{YOUR_FILE_NAME}' not found in this folder.")
        print("   Please check the name or move the file here.")
        return

    print(f"--- 🚀 Starting Test with file: {YOUR_FILE_NAME} ---")

    # 2. Upload File to API
    try:
        with open(YOUR_FILE_NAME, "rb") as f:
            print("📤 Uploading to Aggregation Engine...")
            response = requests.post(
                f"{API_URL}/batch-aggregate", 
                files={"file": f}
            )
            
        if response.status_code != 200:
            print(f"❌ API Error: {response.text}")
            return

        data = response.json()
        batch_id = data["batch_id"]
        total_rows = data["total_items"]
        print(f"✅ Upload Success! Batch ID: {batch_id}")
        print(f"📦 Processing {total_rows} products...")

    except requests.exceptions.ConnectionError:
        print("❌ Error: API is not running.")
        print("   Run: uvicorn app.main:app --reload")
        return

    start_time = time.time()
    print("\n⏳ Tracking Progress (Search -> Extract -> LLM)...")

    while True:
        try:
            res = requests.get(f"{API_URL}/batch-status/{batch_id}")
            if res.status_code != 200:
                print("❌ Error fetching status")
                break
            
            status_data = res.json()
            status = status_data["status"]
            progress = status_data.get("progress", "0/0")
            
            elapsed = int(time.time() - start_time)
            sys.stdout.write(f"\r🔹 Status: {status.upper()} | Progress: {progress} | Time: {elapsed}s")
            sys.stdout.flush()

            if status == "completed":
                print("\n\n🎉 PROCESSING COMPLETE!")
                print("="*50)
                
                output_path = status_data.get("excel_file")
                print(f"📂 Result Saved At: {output_path}")
                
                if os.path.exists(output_path):
                    df = pd.read_excel(output_path)
                    print(f"📊 Final Count: {len(df)} rows generated.")
                    print("✅ You can open the file now.")
                else:
                    print(f"⚠️ Warning: API says file is at {output_path}, but I can't find it locally.")
                break
            
            time.sleep(2)

        except KeyboardInterrupt:
            print("\n🛑 Stopped by user.")
            break

if __name__ == "__main__":
    run_test()
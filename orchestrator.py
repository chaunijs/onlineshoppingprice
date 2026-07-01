import os
import sys
import traceback
import papermill as pm

# List your notebooks exactly as named in the repo in sequential order
notebooks = [
    {"name": "7_eleven_allonline.ipynb", "output": "output/7_eleven_out.ipynb"},
    {"name": "Makro_scraping.ipynb", "output": "output/Makro_out.ipynb"},
    {"name": "Tops__cloudflare.ipynb", "output": "output/Tops_out.ipynb"},
    {"name": "big_C_cloudflare.ipynb", "output": "output/BigC_out.ipynb"},
    {"name": "lotus_online_scraping.ipynb", "output": "output/Lotus_out.ipynb"}
]

# Ensure output directory exists for the executed versions
os.makedirs("output", exist_ok=True)

print("🚀 Starting Sequential Scraper Pipeline...")
failed_jobs = []

for nb in notebooks:
    print("\n" + "="*50)
    print(f"▶️ RUNNING: {nb['name']}")
    print("="*50)
    
    try:
        # Run the notebook using papermill
        pm.execute_notebook(
            input_path=nb['name'],
            output_path=nb['output']
        )
        print(f"✅ SUCCESS: {nb['name']} completed cleanly.")
        
    except Exception as e:
        print(f"❌ ERROR: {nb['name']} failed!")
        print("\n--- ERROR DETAILS ---")
        # Captures the notebook traceback error cleanly in the terminal
        traceback.print_exc()
        print("----------------------\n")
        
        # Log it to our summary list so we know what failed at the end
        failed_jobs.append({"notebook": nb['name'], "error": str(e)})
        print("⏭️ Skipping to next notebook...")

# Pipeline Summary Reporting
print("\n" + "="*50)
print("🏁 PIPELINE RUN COMPLETE SUMMARY")
print("="*50)
if failed_jobs:
    print(f"⚠️ Done, but {len(failed_jobs)} job(s) failed:")
    for failure in failed_jobs:
        print(f" - {failure['notebook']}")
    # Optional: Exit with code 1 if you want GitHub Actions to show a yellow/red status badge
    # sys.exit(1) 
else:
    print("🎉 All notebooks executed successfully with zero errors!")

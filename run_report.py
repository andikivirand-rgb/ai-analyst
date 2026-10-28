import subprocess
import sys

# Windows consoles default to cp1252, which can't encode the emoji used in
# status prints below (and in the submodules run via subprocess) - force utf-8
# so a cosmetic print never crashes an otherwise-successful run.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def run_script(module_name):
    print(f"\n{'='*50}")
    print(f"🚀 Running module: {module_name}")
    print(f"{'='*50}\n")
    
    try:
        # Run the script as a module using the -m flag
        result = subprocess.run([sys.executable, "-m", module_name], check=True)
        print(f"\n✅ Successfully completed: {module_name}")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error: {module_name} failed with exit code {e.returncode}.")
        print("Halting the workflow.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error while running {module_name}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    print("Starting Automated Reporting Workflow...")
    
    # Define the execution order using Python module notation (dots instead of slashes)
    modules_to_run = [
        "src.processing.brand_daily_kpis",
        "src.generators.weekly_player_kpis",
        "src.generators.weekly_report_generator",
        #"src.generators.monthly_report_generator",
        "src.generators.ytd_report_generator"
    ]
    
    for module in modules_to_run:
        run_script(module)
        
    print(f"\n{'='*50}")
    print("🎉 All reports generated successfully! You can now open your HTML dashboards.")
    print(f"{'='*50}\n")
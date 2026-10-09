"""Run: uv run local_test.py --demo ["question"]   (omit --demo for real account data)"""
# /// script
# requires-python = ">=3.12"
# dependencies = ["strands-agents", "boto3"]
# ///
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))
import agent

QUESTIONS = ["Why did my bill go up?", "Am I paying for anything idle?", "What's my biggest cost this month?"]

args = [a for a in sys.argv[1:] if a != "--demo"]
demo = "--demo" in sys.argv
for q in args or QUESTIONS:
    print(f"\n=== {q}  (demo={demo})")
    r = agent.ask(q, demo=demo)
    print(r["answer"])
    print(f"-- valid={agent.is_valid(r['answer'])} tools_used={r['tools_used']}")

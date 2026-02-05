#!/usr/bin/env python3
"""
PHASE 4: Regression test that asserts pipeline counters.
"""
import subprocess
import sys
import os

def main():
    """Run local bridge test and assert counters."""
    print("=" * 60)
    print("PHASE 4: Bridge Counts Assertion Test")
    print("=" * 60)
    
    # Run the local dummy test
    result = subprocess.run(
        ["python", "scripts/test_bridge_local_dummy.py"],
        cwd="/workspace/personaplex",
        capture_output=True,
        text=True,
        timeout=35
    )
    
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    
    # Read bridge log to extract counters
    bridge_log_path = "/workspace/personaplex/logs/bridge.log"
    if not os.path.exists(bridge_log_path):
        print("ERROR: Bridge log not found")
        return 1
    
    with open(bridge_log_path, "r") as f:
        lines = f.readlines()
        last_200 = lines[-200:] if len(lines) > 200 else lines
    
    # Extract final stats
    final_stats = None
    for line in reversed(last_200):
        if "Stats:" in line and "exotel_in" in line:
            final_stats = line
            break
    
    if not final_stats:
        print("ERROR: Could not find final stats in bridge log")
        print("Last 50 lines of bridge log:")
        print("".join(last_200[-50:]))
        return 1
    
    print("\nFinal Stats:", final_stats)
    
    # Parse counters
    import re
    exotel_in_frames = int(re.search(r'exotel_in=(\d+)f', final_stats).group(1)) if re.search(r'exotel_in=(\d+)f', final_stats) else 0
    engine_out_frames = int(re.search(r'engine_out=(\d+)f', final_stats).group(1)) if re.search(r'engine_out=(\d+)f', final_stats) else 0
    engine_in_audio_frames = int(re.search(r'engine_audio=(\d+)f', final_stats).group(1)) if re.search(r'engine_audio=(\d+)f', final_stats) else 0
    exotel_out_frames = int(re.search(r'exotel_out=(\d+)f', final_stats).group(1)) if re.search(r'exotel_out=(\d+)f', final_stats) else 0
    
    print(f"\nCounters:")
    print(f"  exotel_in_frames: {exotel_in_frames} (expected >= 20)")
    print(f"  engine_out_frames: {engine_out_frames} (expected >= 10)")
    print(f"  engine_in_audio_frames: {engine_in_audio_frames} (expected > 0)")
    print(f"  exotel_out_frames: {exotel_out_frames} (expected > 0)")
    
    # Assertions
    failures = []
    if exotel_in_frames < 20:
        failures.append(f"exotel_in_frames={exotel_in_frames} < 20")
    if engine_out_frames < 10:
        failures.append(f"engine_out_frames={engine_out_frames} < 10 (CRITICAL: was 2 before fix)")
    if engine_in_audio_frames == 0:
        failures.append(f"engine_in_audio_frames={engine_in_audio_frames} == 0")
    if exotel_out_frames == 0:
        failures.append(f"exotel_out_frames={exotel_out_frames} == 0")
    
    if failures:
        print("\n" + "=" * 60)
        print("ASSERTION FAILURES:")
        print("=" * 60)
        for failure in failures:
            print(f"  ✗ {failure}")
        print("\nLast 200 lines of bridge.log:")
        print("".join(last_200))
        return 1
    else:
        print("\n" + "=" * 60)
        print("✓ ALL ASSERTIONS PASSED")
        print("=" * 60)
        return 0

if __name__ == "__main__":
    sys.exit(main())

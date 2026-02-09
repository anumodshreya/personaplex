
import subprocess
import numpy as np
import time
import os
import fcntl

def set_nonblocking(f):
    fd = f.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

def test_ffmpeg_incremental():
    # 200ms chunk (24k * 0.2 * 2 = 9600 bytes)
    chunk = np.zeros(9600, dtype=np.int16).tobytes()
    
    cmd = [

        "ffmpeg", "-y", "-hide_banner", "-loglevel", "debug",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "24k", "-application", "voip", "-frame_duration", "20", "-vbr", "off",
        "-flush_packets", "1",
        "-f", "ogg", "-page_duration", "20000", "pipe:1"
    ]
    
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    set_nonblocking(p.stdout)
    set_nonblocking(p.stderr)
    
    print("Started ffmpeg. Writing chunks...")
    
    total_out = 0
    for i in range(5):
        print(f"Writing chunk {i+1}...")
        p.stdin.write(chunk)
        p.stdin.flush()
        
        # Try reading output
        time.sleep(0.5)
        # Read stderr
        try:
            while True:
                err = p.stderr.read(4096)
                if not err: break
                print(f"STDERR: {err.decode(errors='ignore')}")
        except Exception:
            pass

        try:
            out = p.stdout.read(4096)
            if out:
                print(f"Got {len(out)} bytes output")
                total_out += len(out)
            else:
                print("Got None/Empty")
        except Exception as e:
            print(f"Read error: {e}")
            
    p.stdin.close()
    print(f"Closed stdin. Total out: {total_out}")
    
    # Read remainder
    time.sleep(1)
    try:
        out = p.stdout.read()
        if out:
             print(f"Final read: {len(out)} bytes")
    except:
        pass

if __name__ == "__main__":
    test_ffmpeg_incremental()

import os
import sys
import time
import uuid
import json
import asyncio
import argparse
import subprocess
from aiohttp import ClientSession

# Constants
VPS_DATA_DIR = "/root/vps_data"
UBUNTU_TAR_PATH = "/root/ubuntu-base.tar.gz"
UBUNTU_ROOTFS_URL = "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.1-base-amd64.tar.gz"

def download_rootfs():
    if not os.path.exists(UBUNTU_TAR_PATH):
        print("Downloading Ubuntu 22.04 RootFS...")
        import urllib.request
        urllib.request.urlretrieve(UBUNTU_ROOTFS_URL, UBUNTU_TAR_PATH)
        print("Download complete.")
        
def check_proot_status(vps_id):
    try:
        cmd = f"pgrep -f 'proot.*{vps_id}'"
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        return "running" if output else "stopped"
    except Exception:
        return "stopped"

async def async_extract_rootfs(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)
        print(f"Extracting RootFS for {vps_id}...")
        proc = await asyncio.create_subprocess_exec(
            "tar", "-xf", UBUNTU_TAR_PATH, "-C", target_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        
        # Basic DNS and Fake Proc
        resolv_conf = os.path.join(target_dir, "etc", "resolv.conf")
        try:
            os.remove(resolv_conf)
        except OSError:
            pass
        with open(resolv_conf, "w") as f:
            f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")
            
        fake_meminfo = os.path.join(target_dir, ".fake_meminfo")
        with open(fake_meminfo, "w") as f:
            f.write("MemTotal:        2048000 kB\nMemFree:         1024000 kB\n")
            
        fake_cpuinfo = os.path.join(target_dir, ".fake_cpuinfo")
        with open(fake_cpuinfo, "w") as f:
            f.write("processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\n")
            
        fake_hostname = os.path.join(target_dir, ".fake_hostname")
        with open(fake_hostname, "w") as f:
            f.write("SwapiHost\n")
            
        print(f"RootFS ready for {vps_id}")
    return target_dir

async def async_proot_start(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    cmd = f"proot -0 -r {target_dir} -b /dev -b /proc -b /etc/resolv.conf:/etc/resolv.conf -b {target_dir}/.fake_meminfo:/proc/meminfo -b {target_dir}/.fake_cpuinfo:/proc/cpuinfo -b {target_dir}/.fake_hostname:/proc/sys/kernel/hostname -w /root /bin/bash -c 'if ! command -v tmate &> /dev/null; then apt-get update >/dev/null && apt-get install -y tmate curl wget sudo >/dev/null; fi && tmate -F'"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    return proc

async def async_proot_stop(vps_id):
    try:
        cmd = f"pkill -f 'proot.*{vps_id}'"
        await asyncio.create_subprocess_shell(cmd)
        return True
    except Exception:
        return False

async def async_proot_rm(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    if os.path.exists(target_dir):
        cmd = f"rm -rf {target_dir}"
        await asyncio.create_subprocess_shell(cmd)

async def capture_ssh_session_line(proc):
    ssh_line = None
    try:
        start_time = time.time()
        while time.time() - start_time < 90:
            if proc.stdout.at_eof():
                break
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            if not line:
                break
            line_str = line.decode('utf-8').strip()
            print(f"tmate log: {line_str}")
            if "ssh " in line_str and "ro" not in line_str:
                parts = line_str.split()
                for i, part in enumerate(parts):
                    if part == "ssh":
                        ssh_line = " ".join(parts[i:i+2])
                        break
            if ssh_line:
                break
    except Exception as e:
        print(f"Error capturing tmate output: {e}")
    return ssh_line

def get_hardware_info():
    import shutil
    try: cpu = str(os.cpu_count()) + " Cores"
    except: cpu = "Unknown"
    
    try:
        total_disk = shutil.disk_usage("/").total
        disk = str(round(total_disk / (1024**3))) + " GB"
    except: disk = "Unknown"
    
    ram = "Unknown"
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if "MemTotal" in line:
                    kb = int(line.split()[1])
                    gb = max(1, round(kb / (1024**2)))
                    ram = str(gb) + " GB"
                    break
    except: pass
    
    return {"ram": ram, "cpu": cpu, "disk": disk}

async def register_node(master_url):
    master_url = master_url.rstrip("/")
    node_file = ".node_name"
    if os.path.exists(node_file):
        with open(node_file, "r") as f:
            node_name = f.read().strip()
    else:
        node_name = os.uname().nodename + "-" + str(uuid.uuid4())[:4]
        with open(node_file, "w") as f:
            f.write(node_name)
            
    hw = get_hardware_info()
    payload = {'name': node_name, 'ram': hw['ram'], 'cpu': hw['cpu'], 'disk': hw['disk']}
    
    async with ClientSession(headers={'ngrok-skip-browser-warning': '1'}) as session:
        try:
            async with session.post(f"{master_url}/register", json=payload) as resp:
                if resp.status != 200:
                    print(f"Master returned {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json()
                print(f"Registered as Node ID: {data['node_id']}")
                return data['node_id']
        except Exception as e:
            print(f"Failed to register to master: {e}")
            return None

async def worker_loop(master_url, node_id):
    master_url = master_url.rstrip("/")
    print("Worker starting. Listening for jobs...")
    async with ClientSession(headers={'ngrok-skip-browser-warning': '1'}) as session:
        while True:
            try:
                async with session.get(f"{master_url}/jobs", params={'node_id': node_id}) as resp:
                    data = await resp.json()
                    job = data.get('job')
                    
                    if job:
                        job_id = job['job_id']
                        vps_id = job['vps_id']
                        action = job['action']
                        print(f"\n---> Received Job: {action.upper()} for VPS {vps_id}")
                        
                        result_text = ""
                        status = "completed"
                        
                        if action == "create":
                            await async_extract_rootfs(vps_id)
                            proc = await async_proot_start(vps_id)
                            ssh_line = await capture_ssh_session_line(proc)
                            if ssh_line:
                                result_text = ssh_line
                            else:
                                status = "failed"
                                result_text = "Failed to capture SSH session"
                                
                        elif action == "start":
                            proc = await async_proot_start(vps_id)
                            ssh_line = await capture_ssh_session_line(proc)
                            if ssh_line:
                                result_text = ssh_line
                            else:
                                status = "failed"
                                result_text = "Failed to capture SSH session"
                                
                        elif action == "stop":
                            await async_proot_stop(vps_id)
                            
                        elif action == "delete":
                            await async_proot_stop(vps_id)
                            await async_proot_rm(vps_id)
                            
                        elif action == "restart":
                            await async_proot_stop(vps_id)
                            proc = await async_proot_start(vps_id)
                            ssh_line = await capture_ssh_session_line(proc)
                            if ssh_line:
                                result_text = ssh_line
                            else:
                                status = "failed"
                                result_text = "Failed to capture SSH session"
                                
                        # Post result
                        payload = {'job_id': job_id, 'status': status, 'result': result_text}
                        await session.post(f"{master_url}/jobs/result", json=payload)
                        print(f"Job {job_id} {status}")
            except Exception as e:
                print(f"Error polling master: {e}")
                
            await asyncio.sleep(5)

async def main_worker(master_url):
    node_id = await register_node(master_url)
    if node_id:
        await worker_loop(master_url, node_id)
    else:
        print("Exiting due to registration failure.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True, help="Master Bot URL (e.g. https://xyz.ngrok-free.app)")
    args = parser.parse_args()
    
    os.makedirs(VPS_DATA_DIR, exist_ok=True)
    download_rootfs()
    
    try:
        asyncio.run(main_worker(args.master))
    except KeyboardInterrupt:
        print("Worker stopped.")

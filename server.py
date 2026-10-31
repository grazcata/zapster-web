import asyncio
import os
import json
import shutil
from aiohttp import web
from androidtvremote2 import AndroidTVRemote, certificate_generator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(BASE_DIR, "cert.pem")
KEY_FILE = os.path.join(BASE_DIR, "key.pem")

# Ensure certificates exist
if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
    cert_bytes, key_bytes = certificate_generator.generate_selfsigned_cert("ZapsterWeb")
    with open(CERT_FILE, "wb") as f:
        f.write(cert_bytes)
    with open(KEY_FILE, "wb") as f:
        f.write(key_bytes)

# Active remote connections cache: {ip: AndroidTVRemote}
remotes = {}

DEFAULT_DEVICES = [
    {"ip": "192.168.1.7", "name": "TV Sony Bravia (Salon)"},
    {"ip": "192.168.1.10", "name": "Xiaomi Stick 1080p"}
]

async def get_remote(ip: str) -> AndroidTVRemote:
    if ip in remotes and remotes[ip].is_on is not None:
        return remotes[ip]
    remote = AndroidTVRemote("ZapsterWeb", CERT_FILE, KEY_FILE, ip)
    try:
        await asyncio.wait_for(remote.async_connect(), timeout=4.0)
        remotes[ip] = remote
        return remote
    except Exception as e:
        remotes.pop(ip, None)
        raise e

async def api_devices(request):
    return web.json_response(DEFAULT_DEVICES)

async def api_status(request):
    ip = request.query.get("ip", "192.168.1.7")
    try:
        remote = await get_remote(ip)
        return web.json_response({"online": True, "ip": ip, "is_on": remote.is_on})
    except Exception as e:
        return web.json_response({"online": False, "ip": ip, "error": str(e)})

async def api_pair_start(request):
    data = await request.json()
    ip = data.get("ip", "192.168.1.7")
    remote = AndroidTVRemote("ZapsterWeb", CERT_FILE, KEY_FILE, ip)
    remotes[ip] = remote
    try:
        await asyncio.wait_for(remote.async_start_pairing(), timeout=8.0)
        return web.json_response({"success": True, "message": "Code affiché sur la TV"})
    except Exception as e:
        remotes.pop(ip, None)
        return web.json_response({"success": False, "error": str(e)}, status=400)

async def api_pair_finish(request):
    data = await request.json()
    ip = data.get("ip", "192.168.1.7")
    code = data.get("code", "").strip()
    remote = remotes.get(ip)
    if not remote:
        return web.json_response({"success": False, "error": "Appairage non initié"}, status=400)
    try:
        await asyncio.wait_for(remote.async_finish_pairing(code), timeout=8.0)
        await asyncio.wait_for(remote.async_connect(), timeout=4.0)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)

async def api_key(request):
    data = await request.json()
    ip = data.get("ip", "192.168.1.7")
    key = data.get("key")

    key_aliases = {
        "PROG_RED": 183,
        "PROG_GREEN": 184,
        "PROG_YELLOW": 185,
        "PROG_BLUE": 186,
        "CHANNEL_UP": 166,
        "CHANNEL_DOWN": 167,
        "INFO": 165,
    }
    target_key = key_aliases.get(key, key)

    try:
        remote = await get_remote(ip)
        remote.send_key_command(target_key)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_text(request):
    data = await request.json()
    ip = data.get("ip", "192.168.1.7")
    text = data.get("text", "")
    press_enter = data.get("enter", False)
    try:
        remote = await get_remote(ip)
        if text:
            remote.send_text(text)
        if press_enter:
            await asyncio.sleep(0.2)
            remote.send_key_command("DPAD_CENTER")
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_app(request):
    data = await request.json()
    ip = data.get("ip", "192.168.1.7")
    link = data.get("link")
    try:
        remote = await get_remote(ip)
        remote.send_launch_app_command(link)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_upload_apk(request):
    reader = await request.multipart()
    ip = "192.168.1.7"
    temp_apk = os.path.join(BASE_DIR, "uploaded_app.apk")

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "ip":
            ip = (await part.text()).strip()
        elif part.name == "apk":
            with open(temp_apk, "wb") as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)

    if not os.path.exists(temp_apk):
        return web.json_response({"success": False, "error": "Aucun fichier reçu"}, status=400)

    # 1. Try direct ADB install via tv.py or python adb-shell
    adb_installed = False
    cmd = f"TV_HOST={ip} /home/graz/workspace/projects/cleanyourtv/tools/tv.py 'pm install -r /data/local/tmp/app.apk'"
    push_cmd = f"TV_HOST={ip} /home/graz/workspace/venvs/myenv/bin/python /home/graz/workspace/projects/cleanyourtv/tools/b64push.py {temp_apk} /data/local/tmp/app.apk"
    proc_push = await asyncio.create_subprocess_shell(push_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc_push.communicate()
    if proc_push.returncode == 0:
        proc_install = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc_install.communicate()
        if proc_install.returncode == 0 and b"Success" in out:
            adb_installed = True

    if adb_installed:
        return web.json_response({"success": True, "message": "APK installé avec succès sur la TV !"})

    # 2. Fallback: return direct local URL and trigger launch on TV
    local_url = f"http://192.168.1.66:8090/static/uploaded_app.apk"
    try:
        shutil.copyfile(temp_apk, os.path.join(BASE_DIR, "static", "uploaded_app.apk"))
        remote = await get_remote(ip)
        remote.send_launch_app_command(local_url)
    except Exception as e:
        pass

    return web.json_response({
        "success": True,
        "message": f"Fichier disponible au téléchargement : {local_url}",
        "url": local_url
    })

async def index(request):
    return web.FileResponse(os.path.join(BASE_DIR, "static", "index.html"))

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/api/devices", api_devices)
app.router.add_get("/api/status", api_status)
app.router.add_post("/api/pair/start", api_pair_start)
app.router.add_post("/api/pair/finish", api_pair_finish)
app.router.add_post("/api/key", api_key)
app.router.add_post("/api/text", api_text)
app.router.add_post("/api/app", api_app)
app.router.add_post("/api/upload_apk", api_upload_apk)
app.router.add_static("/static/", os.path.join(BASE_DIR, "static"))

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8090)

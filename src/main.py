import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

CHANNEL = "jotta"

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
DISCORD_WEBHOOK = os.getenv("DISCORD_CLIP_WEBHOOK_URL")

STATE_FILE = "data/state.json"

# Procurar clips dos últimos 10 minutos
LOOKBACK_MINUTES = 10


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def load_state():

    if not os.path.exists(STATE_FILE):
        return {"seen_clips": []}

    with open(
        STATE_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def save_state(state):

    os.makedirs("data", exist_ok=True)

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )


def get_token():

    r = requests.post(
        TWITCH_TOKEN_URL,
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=30
    )

    print(
        f"🔐 Twitch OAuth: HTTP {r.status_code}"
    )

    r.raise_for_status()

    return r.json()["access_token"]


def twitch_get(endpoint, token, params):

    r = requests.get(
        f"{TWITCH_API}/{endpoint}",
        headers={
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {token}"
        },
        params=params,
        timeout=30
    )

    print(
        f"🌐 Twitch {endpoint}: HTTP {r.status_code}"
    )

    r.raise_for_status()

    return r.json()


def get_channel_id(token):

    data = twitch_get(
        "users",
        token,
        {
            "login": CHANNEL
        }
    )

    users = data.get("data", [])

    if not users:
        raise RuntimeError(
            f"Canal {CHANNEL} não encontrado."
        )

    return users[0]["id"]


def get_clips(token, channel_id):

    data = twitch_get(
        "clips",
        token,
        {
            "broadcaster_id": channel_id,
            "first": 100
        }
    )

    return data.get("data", [])


def send_discord(clip):

    payload = {
        "username": "JOTTA Clip Watcher",
        "content": "🎬 **NOVO CLIP DO JOTTA**",
        "embeds": [
            {
                "title": clip.get(
                    "title",
                    "Novo clip"
                ),
                "url": clip["url"],
                "description": (
                    f"📺 **Streamer:** {CHANNEL}\n"
                    f"✂️ **Criado por:** "
                    f"{clip.get('creator_name', 'Desconhecido')}\n"
                    f"👁️ **Visualizações:** "
                    f"{clip.get('view_count', 0)}\n"
                    f"🕐 **Publicado:** "
                    f"{clip['created_at']}"
                )
            }
        ]
    }

    print("📨 A enviar para Discord...")

    for attempt in range(5):

        r = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=30
        )

        print(
            f"📡 Discord: HTTP {r.status_code}"
        )

        if r.status_code in (200, 204):
            print("✅ Discord recebeu o clip.")
            return True

        if r.status_code == 429:

            try:
                retry = float(
                    r.json().get(
                        "retry_after",
                        2
                    )
                )
            except Exception:
                retry = 2

            print(
                f"⏳ Discord rate limit. "
                f"A aguardar {retry} segundos."
            )

            time.sleep(
                retry + 0.5
            )

            continue

        print(
            f"❌ Discord respondeu: {r.text}"
        )

        return False

    return False


def main():

    print("=" * 70)
    print("🎬 JOTTA CLIP WATCHER")
    print("=" * 70)

    current_time = now_utc()

    cutoff = (
        current_time
        - timedelta(
            minutes=LOOKBACK_MINUTES
        )
    )

    print(
        f"🕐 AGORA UTC: "
        f"{current_time.isoformat()}"
    )

    print(
        f"⏰ LIMITE UTC: "
        f"{cutoff.isoformat()}"
    )

    print(
        f"🔎 Janela: últimos "
        f"{LOOKBACK_MINUTES} minutos"
    )

    state = load_state()

    seen = set(
        state.get(
            "seen_clips",
            []
        )
    )

    print(
        f"🧠 Clips já enviados: "
        f"{len(seen)}"
    )

    token = get_token()

    channel_id = get_channel_id(
        token
    )

    print(
        f"🆔 JOTTA ID: {channel_id}"
    )

    clips = get_clips(
        token,
        channel_id
    )

    print(
        f"📦 Twitch devolveu "
        f"{len(clips)} clips"
    )

    print("-" * 70)

    new_count = 0

    for clip in clips:

        clip_id = clip["id"]

        created = parse_time(
            clip["created_at"]
        )

        age = (
            current_time - created
        ).total_seconds() / 60

        print(
            f"🎞️ {clip['title']}"
        )

        print(
            f"   ID: {clip_id}"
        )

        print(
            f"   Criado: {clip['created_at']}"
        )

        print(
            f"   Idade: {age:.2f} minutos"
        )

        if clip_id in seen:

            print(
                "   ⏭️ JÁ ENVIADO"
            )

            print("-" * 70)

            continue

        if created < cutoff:

            print(
                "   ⏭️ MAIS ANTIGO QUE 10 MINUTOS"
            )

            print("-" * 70)

            continue

        print(
            "   🟢 NOVO CLIP DETETADO!"
        )

        success = send_discord(
            clip
        )

        if success:

            seen.add(
                clip_id
            )

            new_count += 1

            print(
                "   💾 ID guardado."
            )

        else:

            print(
                "   ❌ Não foi guardado porque "
                "o Discord falhou."
            )

        print("-" * 70)

    state["seen_clips"] = list(
        seen
    )

    save_state(
        state
    )

    print("=" * 70)

    print(
        f"🆕 NOVOS CLIPS: {new_count}"
    )

    print(
        f"🧠 TOTAL GUARDADO: {len(seen)}"
    )

    print("=" * 70)


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        print(
            f"❌ ERRO: {error}"
        )

        sys.exit(1)

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"

CHANNEL_LOGIN = os.getenv("TWITCH_CHANNEL", "jotta")

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

DISCORD_CLIP_WEBHOOK_URL = os.getenv("DISCORD_CLIP_WEBHOOK_URL")

STATE_FILE = "data/state.json"

LOOKBACK_MINUTES = 15
DISCORD_RETRIES = 5


def utc_now():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "seen_clips": [],
            "last_status_at": None
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        state.setdefault("seen_clips", [])
        state.setdefault("last_status_at", None)

        return state

    except Exception:
        print("⚠️ Não foi possível ler o state.json. A iniciar estado novo.")

        return {
            "seen_clips": [],
            "last_status_at": None
        }


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def get_twitch_access_token():
    response = requests.post(
        TWITCH_AUTH_URL,
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=30
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Twitch OAuth falhou: HTTP {response.status_code}: {response.text}"
        )

    return response.json()["access_token"]


def twitch_request(endpoint, token, params=None):
    headers = {
        "Client-ID": CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }

    response = requests.get(
        f"{TWITCH_API_BASE}/{endpoint}",
        headers=headers,
        params=params,
        timeout=30
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Twitch API falhou: HTTP {response.status_code}: {response.text}"
        )

    return response.json()


def get_broadcaster_id(token):
    data = twitch_request(
        "users",
        token,
        params={
            "login": CHANNEL_LOGIN
        }
    )

    users = data.get("data", [])

    if not users:
        raise RuntimeError(
            f"O canal Twitch '{CHANNEL_LOGIN}' não foi encontrado."
        )

    return users[0]["id"]


def get_recent_clips(token, broadcaster_id, cutoff):
    clips = []

    cursor = None

    # Até 5 páginas de 100 clips.
    # Normalmente a primeira página será mais do que suficiente.
    for _ in range(5):

        params = {
            "broadcaster_id": broadcaster_id,
            "first": 100
        }

        if cursor:
            params["after"] = cursor

        data = twitch_request(
            "clips",
            token,
            params=params
        )

        page = data.get("data", [])

        if not page:
            break

        clips.extend(page)

        # Os clips vêm dos mais recentes para os mais antigos.
        oldest = min(
            parse_datetime(clip["created_at"])
            for clip in page
        )

        if oldest < cutoff:
            break

        cursor = data.get("pagination", {}).get("cursor")

        if not cursor:
            break

    return clips


def send_discord(webhook_url, payload):
    if not webhook_url:
        raise RuntimeError("DISCORD_CLIP_WEBHOOK_URL não está configurado.")

    for attempt in range(1, DISCORD_RETRIES + 1):

        response = requests.post(
            webhook_url,
            json=payload,
            timeout=30
        )

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:

            retry_after = None

            try:
                body = response.json()
                retry_after = body.get("retry_after")
            except Exception:
                pass

            if retry_after is None:
                header = response.headers.get("Retry-After")

                if header:
                    try:
                        retry_after = float(header)
                    except ValueError:
                        retry_after = None

            if retry_after is None:
                retry_after = min(2 ** attempt, 30)

            retry_after = float(retry_after) + 0.5

            print(
                f"⚠️ Discord rate limit (429). "
                f"Tentativa {attempt}/{DISCORD_RETRIES}. "
                f"A aguardar {retry_after:.1f}s..."
            )

            time.sleep(retry_after)
            continue

        if response.status_code >= 500:
            wait = min(2 ** attempt, 30)

            print(
                f"⚠️ Discord HTTP {response.status_code}. "
                f"A aguardar {wait}s..."
            )

            time.sleep(wait)
            continue

        raise RuntimeError(
            f"Discord webhook falhou: "
            f"HTTP {response.status_code}: {response.text}"
        )

    raise RuntimeError(
        "Discord continuou a aplicar rate limit depois de várias tentativas."
    )


def create_clip_payload(clip):
    title = clip.get("title") or "Sem título"

    creator_name = clip.get("creator_name") or "Desconhecido"

    view_count = clip.get("view_count", 0)

    clip_url = clip.get("url")

    created_at = clip.get("created_at")

    return {
        "username": "JOTTA Clip Watcher",
        "content": "🎬 **NOVO CLIP DO JOTTA**",
        "embeds": [
            {
                "title": title,
                "url": clip_url,
                "description": (
                    f"📺 **Streamer:** {CHANNEL_LOGIN}\n"
                    f"✂️ **Criado por:** {creator_name}\n"
                    f"👁️ **Visualizações:** {view_count}\n"
                    f"🕐 **Criado:** {created_at}"
                )
            }
        ]
    }


def main():
    if not CLIENT_ID:
        raise RuntimeError("TWITCH_CLIENT_ID não está configurado.")

    if not CLIENT_SECRET:
        raise RuntimeError("TWITCH_CLIENT_SECRET não está configurado.")

    if not DISCORD_CLIP_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_CLIP_WEBHOOK_URL não está configurado."
        )

    state = load_state()

    now = utc_now()

    cutoff = now - timedelta(minutes=LOOKBACK_MINUTES)

    print("=" * 60)
    print("🎬 JOTTA TWITCH CLIP WATCHER")
    print("=" * 60)

    print(f"📺 Canal: {CHANNEL_LOGIN}")
    print(f"🕐 Hora UTC: {now.isoformat()}")
    print(f"🔎 Janela: últimos {LOOKBACK_MINUTES} minutos")

    token = get_twitch_access_token()

    broadcaster_id = get_broadcaster_id(token)

    print(f"🆔 Broadcaster ID: {broadcaster_id}")

    clips = get_recent_clips(
        token,
        broadcaster_id,
        cutoff
    )

    print(f"🔎 Clips recebidos da Twitch: {len(clips)}")

    seen_clips = set(state.get("seen_clips", []))

    # Primeira execução:
    # não enviamos os clips antigos existentes antes de instalar o sistema.
    if not state.get("seen_clips"):
        print("🆕 Primeira execução.")

        for clip in clips:
            seen_clips.add(clip["id"])

        state["seen_clips"] = list(seen_clips)

        save_state(state)

        print(
            f"✅ {len(clips)} clips existentes marcados como vistos."
        )
        print("📨 Nenhuma notificação enviada nesta primeira execução.")

        return

    new_clips = []

    for clip in clips:

        clip_id = clip["id"]

        created_at = parse_datetime(
            clip["created_at"]
        )

        if created_at < cutoff:
            continue

        if clip_id in seen_clips:
            continue

        new_clips.append(clip)

    # Mais antigos primeiro
    new_clips.sort(
        key=lambda clip: parse_datetime(clip["created_at"])
    )

    print(f"🆕 Clips novos encontrados: {len(new_clips)}")

    notifications_sent = 0

    for clip in new_clips:

        clip_id = clip["id"]

        print(
            f"🎬 Novo clip: {clip.get('title', 'Sem título')}"
        )

        payload = create_clip_payload(clip)

        try:
            send_discord(
                DISCORD_CLIP_WEBHOOK_URL,
                payload
            )

            # Só marcamos como visto DEPOIS de enviar com sucesso.
            seen_clips.add(clip_id)

            notifications_sent += 1

            print("   ✅ Enviado para Discord.")

            # Pequena pausa para evitar vários pedidos seguidos.
            time.sleep(1)

        except Exception as error:

            print(
                f"   ❌ Não foi possível enviar o clip {clip_id}: {error}"
            )

            # Não adicionamos ao seen_clips.
            # Assim será tentado novamente na próxima execução.

    state["seen_clips"] = list(seen_clips)

    # Mantém apenas os últimos 5000 IDs para não deixar o ficheiro crescer
    # indefinidamente.
    if len(state["seen_clips"]) > 5000:

        state["seen_clips"] = state["seen_clips"][-5000:]

    save_state(state)

    print("=" * 60)
    print(
        f"📊 Resultado: {notifications_sent} notificações enviadas."
    )
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:

        print("=" * 60)
        print("❌ CHECK FALHOU")
        print("=" * 60)

        print(str(error))

        sys.exit(1)

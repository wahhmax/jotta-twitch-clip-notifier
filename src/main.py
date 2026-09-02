import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "jotta")

DISCORD_CLIP_WEBHOOK_URL = os.environ["DISCORD_CLIP_WEBHOOK_URL"]
DISCORD_STATUS_WEBHOOK_URL = os.environ["DISCORD_STATUS_WEBHOOK_URL"]

DATA_FILE = Path("data/seen_clips.json")

CLIPS_TO_CHECK = 100


def twitch_request(url, headers=None, method="GET", data=None):
    request = Request(
        url,
        headers=headers or {},
        method=method
    )

    if data is not None:
        request.data = data

    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Twitch API HTTP {error.code}: {body}"
        )

    except URLError as error:
        raise RuntimeError(
            f"Erro de ligação à Twitch API: {error}"
        )


def get_app_access_token():
    params = urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"
    })

    url = f"https://id.twitch.tv/oauth2/token?{params}"

    response = twitch_request(
        url,
        method="POST"
    )

    if "access_token" not in response:
        raise RuntimeError(
            f"Não foi possível obter o token Twitch: {response}"
        )

    return response["access_token"]


def get_broadcaster(token):
    params = urlencode({
        "login": TWITCH_CHANNEL
    })

    url = f"https://api.twitch.tv/helix/users?{params}"

    response = twitch_request(
        url,
        headers={
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
    )

    users = response.get("data", [])

    if not users:
        raise RuntimeError(
            f"O canal Twitch '{TWITCH_CHANNEL}' não foi encontrado."
        )

    return users[0]


def get_clips(token, broadcaster_id):
    params = urlencode({
        "broadcaster_id": broadcaster_id,
        "first": CLIPS_TO_CHECK
    })

    url = f"https://api.twitch.tv/helix/clips?{params}"

    response = twitch_request(
        url,
        headers={
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
    )

    return response.get("data", [])


def load_seen_clips():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not DATA_FILE.exists():
        return set()

    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return set()

        return set(data)

    except Exception:
        return set()


def save_seen_clips(seen):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Guardamos apenas os últimos 1000 IDs.
    # É mais do que suficiente para evitar duplicados.
    latest = list(seen)[-1000:]

    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            latest,
            file,
            ensure_ascii=False,
            indent=2
        )


def send_discord(webhook_url, payload):
    body = json.dumps(payload).encode("utf-8")

    request = Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "JottaTwitchClipNotifier/1.0"
        },
        method="POST"
    )

    try:
        with urlopen(request, timeout=30) as response:
            return response.status

    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")

        raise RuntimeError(
            f"Discord webhook HTTP {error.code}: {body}"
        )


def format_datetime(value):
    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        return dt.astimezone().strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    except Exception:
        return value


def send_clip_notification(clip):
    title = clip.get("title") or "Sem título"
    creator = clip.get("creator_name") or "Desconhecido"
    views = clip.get("view_count", 0)
    created_at = format_datetime(
        clip.get("created_at", "")
    )
    url = clip.get("url", "")

    payload = {
        "username": "JOTTA Clip Watcher",
        "embeds": [
            {
                "title": "🎬 NOVO CLIP DETETADO",
                "url": url,
                "description": (
                    f"Foi encontrado um novo clip no canal **{TWITCH_CHANNEL}**."
                ),
                "fields": [
                    {
                        "name": "📺 Streamer",
                        "value": f"**{TWITCH_CHANNEL}**",
                        "inline": True
                    },
                    {
                        "name": "✂️ Criado por",
                        "value": creator,
                        "inline": True
                    },
                    {
                        "name": "👁️ Visualizações",
                        "value": str(views),
                        "inline": True
                    },
                    {
                        "name": "🎞️ Título",
                        "value": title[:1024],
                        "inline": False
                    },
                    {
                        "name": "🔗 Clip",
                        "value": f"[**ABRIR CLIP NA TWITCH**]({url})",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": f"Criado em {created_at}"
                },
                "timestamp": clip.get("created_at")
            }
        ]
    }

    send_discord(
        DISCORD_CLIP_WEBHOOK_URL,
        payload
    )


def send_status(
    clips_checked,
    new_clips,
    notifications_sent,
    error=None
):
    now = datetime.now(
        timezone.utc
    ).astimezone().strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    if error:
        payload = {
            "username": "JOTTA Clip Watcher",
            "embeds": [
                {
                    "title": "🔴 CHECK FALHOU",
                    "description": (
                        f"O sistema tentou verificar o canal "
                        f"**{TWITCH_CHANNEL}**, mas ocorreu um erro."
                    ),
                    "fields": [
                        {
                            "name": "❌ Erro",
                            "value": str(error)[:1024],
                            "inline": False
                        },
                        {
                            "name": "🕐 Hora",
                            "value": now,
                            "inline": True
                        }
                    ]
                }
            ]
        }

    else:
        payload = {
            "username": "JOTTA Clip Watcher",
            "embeds": [
                {
                    "title": "✅ CHECK EXECUTADO",
                    "description": (
                        f"Verificação concluída para **{TWITCH_CHANNEL}**."
                    ),
                    "fields": [
                        {
                            "name": "📺 Canal",
                            "value": TWITCH_CHANNEL,
                            "inline": True
                        },
                        {
                            "name": "🔎 Clips analisados",
                            "value": str(clips_checked),
                            "inline": True
                        },
                        {
                            "name": "🆕 Clips novos",
                            "value": str(new_clips),
                            "inline": True
                        },
                        {
                            "name": "📨 Notificações enviadas",
                            "value": str(notifications_sent),
                            "inline": True
                        },
                        {
                            "name": "🕐 Hora",
                            "value": now,
                            "inline": True
                        }
                    ],
                    "footer": {
                        "text": "JOTTA Twitch Clip Watcher"
                    }
                }
            ]
        }

    send_discord(
        DISCORD_STATUS_WEBHOOK_URL,
        payload
    )


def main():
    print("=" * 60)
    print("JOTTA TWITCH CLIP WATCHER")
    print("=" * 60)

    seen = load_seen_clips()

    print("A obter token da Twitch...")
    token = get_app_access_token()

    print(f"A procurar canal: {TWITCH_CHANNEL}")
    broadcaster = get_broadcaster(token)

    broadcaster_id = broadcaster["id"]

    print(
        f"Canal encontrado: "
        f"{broadcaster.get('display_name', TWITCH_CHANNEL)} "
        f"({broadcaster_id})"
    )

    print("A procurar clips...")
    clips = get_clips(
        token,
        broadcaster_id
    )

    print(f"Clips recebidos: {len(clips)}")

    # IMPORTANTE:
    # Na primeira execução não queremos enviar todos
    # os clips históricos para o Discord.
    first_run = not DATA_FILE.exists()

    if first_run:
        print(
            "Primeira execução detectada. "
            "A marcar clips existentes como vistos."
        )

        for clip in clips:
            clip_id = clip.get("id")

            if clip_id:
                seen.add(clip_id)

        save_seen_clips(seen)

        send_status(
            clips_checked=len(clips),
            new_clips=0,
            notifications_sent=0
        )

        print("Primeira execução concluída.")
        return

    new_clips = []

    for clip in clips:
        clip_id = clip.get("id")

        if not clip_id:
            continue

        if clip_id not in seen:
            new_clips.append(clip)

    # Os clips são enviados do mais antigo para o mais recente.
    new_clips.sort(
        key=lambda clip: clip.get("created_at", "")
    )

    notifications_sent = 0

    for clip in new_clips:
        clip_id = clip["id"]

        print(
            f"Novo clip encontrado: "
            f"{clip.get('title', 'Sem título')}"
        )

        send_clip_notification(clip)

        seen.add(clip_id)
        notifications_sent += 1

    save_seen_clips(seen)

    send_status(
        clips_checked=len(clips),
        new_clips=len(new_clips),
        notifications_sent=notifications_sent
    )

    print()
    print("CHECK CONCLUÍDO")
    print(f"Clips analisados: {len(clips)}")
    print(f"Clips novos: {len(new_clips)}")
    print(
        f"Notificações enviadas: {notifications_sent}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print()
        print("ERRO:")
        print(error)
        print()

        try:
            send_status(
                clips_checked=0,
                new_clips=0,
                notifications_sent=0,
                error=error
            )
        except Exception as discord_error:
            print(
                f"Também não foi possível enviar "
                f"o erro para o Discord: {discord_error}"
            )

        sys.exit(1)

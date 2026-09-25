import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"

CHANNEL = "jotta"

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

DISCORD_WEBHOOK = os.getenv("DISCORD_CLIP_WEBHOOK_URL")
KICK_WEBHOOK = os.getenv("DISCORD_KICK_CLIP_WEBHOOK_URL")

STATE_FILE = "data/state.json"

# Procuramos clips dentro das últimas 24 horas.
SEARCH_WINDOW_HOURS = 24

# Depois, dentro desses clips, só enviamos os que foram
# criados nos últimos 15 minutos.
RECENT_WINDOW_MINUTES = 15

# Número máximo de páginas.
MAX_PAGES = 10


# ============================================================
# TEMPO
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def format_time(dt):
    return dt.strftime(
        "%d/%m/%Y %H:%M:%S UTC"
    )


# ============================================================
# ESTADO
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "seen_clips": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            state = json.load(file)

        state.setdefault(
            "seen_clips",
            []
        )

        return state

    except (ValueError, OSError) as error:
        raise RuntimeError("Não foi possível ler o histórico de clips.") from error


def save_state(state):

    os.makedirs(
        os.path.dirname(STATE_FILE) or ".",
        exist_ok=True
    )

    with open(
        STATE_FILE + ".tmp",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )

    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ============================================================
# TWITCH AUTH
# ============================================================

def get_access_token():

    response = requests.post(
        TWITCH_AUTH_URL,
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=30
    )

    print(
        f"🔐 Twitch OAuth: HTTP {response.status_code}"
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Twitch OAuth falhou: "
            f"{response.text}"
        )

    return response.json()["access_token"]


# ============================================================
# TWITCH API
# ============================================================

def twitch_get(
    endpoint,
    token,
    params
):

    response = requests.get(
        f"{TWITCH_API}/{endpoint}",
        headers={
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {token}"
        },
        params=params,
        timeout=30
    )

    print(
        f"🌐 Twitch {endpoint}: "
        f"HTTP {response.status_code}"
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Twitch API falhou: "
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

    return response.json()


def get_channel_id(token):

    data = twitch_get(
        "users",
        token,
        {
            "login": CHANNEL
        }
    )

    users = data.get(
        "data",
        []
    )

    if not users:

        raise RuntimeError(
            f"O canal '{CHANNEL}' não foi encontrado."
        )

    return users[0]["id"]


# ============================================================
# OBTER CLIPS DAS ÚLTIMAS 24 HORAS
# ============================================================

def get_last_24h_clips(
    token,
    channel_id
):

    now = now_utc()

    start_time = (
        now
        - timedelta(
            hours=SEARCH_WINDOW_HOURS
        )
    )

    print(
        f"📅 Procurar clips desde: "
        f"{format_time(start_time)}"
    )

    print(
        f"📅 Até: "
        f"{format_time(now)}"
    )

    all_clips = []

    cursor = None

    for page in range(
        1,
        MAX_PAGES + 1
    ):

        params = {

            "broadcaster_id": channel_id,

            # IMPORTANTE:
            # A Twitch vai procurar directamente
            # dentro das últimas 24 horas.
            "started_at": start_time.isoformat(),

            "ended_at": now.isoformat(),

            "first": 100
        }

        if cursor:

            params["after"] = cursor

        data = twitch_get(
            "clips",
            token,
            params
        )

        clips = data.get(
            "data",
            []
        )

        print(
            f"📦 Página {page}: "
            f"{len(clips)} clips"
        )

        all_clips.extend(
            clips
        )

        pagination = data.get(
            "pagination",
            {}
        )

        cursor = pagination.get(
            "cursor"
        )

        if not cursor:

            break

    return all_clips


def get_kick_clips():
    """Endpoint do site da Kick; não faz parte da API pública oficial."""
    clips = []
    ids = set()
    cursor = None
    cursors = set()
    for _ in range(MAX_PAGES):
        params = {"sort": "date", "time": "day"}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(
            f"https://kick.com/api/v2/channels/{CHANNEL}/clips",
            params=params,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Kick: HTTP {response.status_code}. O endpoint pode estar indisponível "
                "ou a bloquear pedidos automáticos."
            )
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
            raise RuntimeError("Kick: formato inesperado na resposta de clips.")
        for clip in data["clips"]:
            clip_id = str(clip["id"])
            if clip_id in ids:
                continue
            ids.add(clip_id)
            clips.append({
                "id": clip_id,
                "title": clip.get("title") or "Novo clip",
                "url": f"https://kick.com/{CHANNEL}?clip={clip_id}",
                "creator_name": (clip.get("creator") or {}).get("username", "Desconhecido"),
                "view_count": clip.get("view_count", clip.get("views", 0)),
                "created_at": clip["created_at"],
            })
        cursor = data.get("nextCursor") or data.get("next_cursor")
        if not cursor:
            return clips
        if cursor in cursors:
            raise RuntimeError("Kick: cursor de paginação repetido.")
        cursors.add(cursor)
    raise RuntimeError("Kick: limite de páginas atingido; pesquisa incompleta.")


def discord_post(webhook, payload):
    payload["allowed_mentions"] = {"parse": []}
    for attempt in range(1, 6):
        try:
            response = requests.post(webhook, json=payload, timeout=30)
        except requests.RequestException:
            # Não imprimir a exceção: pode conter o URL secreto do webhook.
            print("Discord: falha de ligação; envio não confirmado.")
            return False
        if response.status_code in (200, 204):
            return True
        print(f"Discord: HTTP {response.status_code}")
        if response.status_code == 429:
            try:
                delay = float(response.json().get("retry_after", 2 ** attempt)) + 0.5
            except (ValueError, TypeError, AttributeError):
                delay = 2 ** attempt
            # Não bloquear indefinidamente a outra plataforma.
            if delay > 60:
                return False
        elif response.status_code >= 500:
            delay = min(2 ** attempt, 30)
        else:
            return False
        if attempt < 5:
            time.sleep(max(0, delay))
    return False


def send_interval_message(start_time, end_time, clip_count, webhook, platform):
    return discord_post(webhook, {
        "username": f"JOTTA {platform} Clip Watcher",
        "content": (
            f"🕐 **JOTTA {platform} Clip Watcher**\n\n"
            f"Clips entre **{start_time:%H:%M:%S}** e **{end_time:%H:%M:%S} UTC**\n\n"
            f"🎯 **{clip_count} clips novos detetados**"
        ),
    })


def send_to_discord(clip, webhook, platform):
    return discord_post(webhook, {
        "username": f"JOTTA {platform} Clip Watcher",
        "content": f"🎬 **NOVO CLIP DO JOTTA — {platform.upper()}**",
        "embeds": [{
            "title": (clip.get("title") or "Novo clip")[:256],
            "url": clip["url"],
            "color": 0x53FC18 if platform == "Kick" else 0x9146FF,
            "description": (
                f"📺 **Streamer:** {CHANNEL}\n\n"
                f"✂️ **Criado por:** {clip.get('creator_name', 'Desconhecido')}\n\n"
                f"👁️ **Visualizações:** {clip.get('view_count', 0)}\n\n"
                f"🕐 **Publicado:** {clip['created_at']}"
            ),
            "footer": {"text": f"JOTTA {platform} Clip Watcher"},
        }],
    })


def get_twitch_clips():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Configura TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET.")
    token = get_access_token()
    return get_last_24h_clips(token, get_channel_id(token))


def process_platform(platform, webhook, state_key, fetch_clips, state):
    now = now_utc()
    cutoff = now - timedelta(minutes=RECENT_WINDOW_MINUTES)
    history = state.setdefault(state_key, [])
    seen = set(history)
    new_clips = {}
    for clip in fetch_clips():
        created_at = parse_time(clip["created_at"])
        if clip["id"] not in seen and cutoff <= created_at <= now:
            new_clips[clip["id"]] = clip
    clips = sorted(new_clips.values(), key=lambda clip: parse_time(clip["created_at"]))
    print(f"{platform}: {len(clips)} clips novos nos últimos {RECENT_WINDOW_MINUTES} minutos.")
    successful = send_interval_message(cutoff, now, len(clips), webhook, platform)
    for clip in clips:
        if send_to_discord(clip, webhook, platform):
            history.append(clip["id"])
            state[state_key] = history[-5000:]
            # Guardar cada envio confirmado, mesmo se o seguinte falhar.
            save_state(state)
        else:
            successful = False
        time.sleep(1)
    if not successful:
        raise RuntimeError(f"{platform}: um ou mais envios para o Discord falharam.")


def main():
    state = load_state()
    failures = []
    # Manter a chave antiga da Twitch evita reenviar o histórico existente.
    platforms = [
        ("Twitch", DISCORD_WEBHOOK, "seen_clips", get_twitch_clips),
        ("Kick", KICK_WEBHOOK, "kick_seen_clips", get_kick_clips),
    ]
    for platform, webhook, state_key, fetch_clips in platforms:
        if platform == "Kick" and not webhook:
            print("Kick desativada: configura DISCORD_KICK_CLIP_WEBHOOK_URL para ativar.")
            continue
        try:
            if not webhook:
                raise RuntimeError("DISCORD_CLIP_WEBHOOK_URL não configurado.")
            process_platform(platform, webhook, state_key, fetch_clips, state)
        except Exception as error:
            # HTTP exceptions podem incluir URLs secretos; só expor erros controlados.
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print(f"{platform}: falhou — {detail}")
            failures.append(platform)
        finally:
            save_state(state)
    if failures:
        raise RuntimeError("Falha nas plataformas: " + ", ".join(failures))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CHECK FALHOU: {type(error).__name__}")
        sys.exit(1)



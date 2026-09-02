import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"

CHANNEL_LOGIN = "jotta"

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
DISCORD_WEBHOOK = os.getenv("DISCORD_CLIP_WEBHOOK_URL")

STATE_FILE = "data/state.json"

# O GitHub executa a cada 10 minutos.
# Procuramos 15 minutos para trás para compensar atrasos.
LOOKBACK_MINUTES = 15

# Número máximo de tentativas quando o Discord responde 429.
MAX_DISCORD_RETRIES = 5


# ============================================================
# TEMPO
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def parse_twitch_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
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

        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if "seen_clips" not in state:
            state["seen_clips"] = []

        return state

    except Exception as error:

        print(f"⚠️ Erro a ler state.json: {error}")

        return {
            "seen_clips": []
        }


def save_state(state):

    os.makedirs("data", exist_ok=True)

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# TWITCH
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

    if response.status_code != 200:

        raise RuntimeError(
            f"Erro ao obter token Twitch: "
            f"HTTP {response.status_code} "
            f"{response.text}"
        )

    return response.json()["access_token"]


def twitch_get(endpoint, token, params=None):

    response = requests.get(
        f"{TWITCH_API_BASE}/{endpoint}",
        headers={
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {token}"
        },
        params=params,
        timeout=30
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Twitch API erro: "
            f"HTTP {response.status_code} "
            f"{response.text}"
        )

    return response.json()


def get_broadcaster_id(token):

    data = twitch_get(
        "users",
        token,
        {
            "login": CHANNEL_LOGIN
        }
    )

    users = data.get("data", [])

    if not users:

        raise RuntimeError(
            f"O canal '{CHANNEL_LOGIN}' não foi encontrado."
        )

    return users[0]["id"]


def get_clips(token, broadcaster_id):

    now = utc_now()

    cutoff = now - timedelta(
        minutes=LOOKBACK_MINUTES
    )

    all_clips = []

    cursor = None

    # Faz até 5 páginas.
    # Cada página pode ter até 100 clips.
    for page_number in range(5):

        params = {
            "broadcaster_id": broadcaster_id,
            "first": 100
        }

        if cursor:
            params["after"] = cursor

        data = twitch_get(
            "clips",
            token,
            params
        )

        clips = data.get("data", [])

        if not clips:
            break

        all_clips.extend(clips)

        # Verifica o clip mais antigo desta página.
        oldest_clip = min(
            parse_twitch_time(
                clip["created_at"]
            )
            for clip in clips
        )

        # Como os clips vêm dos mais recentes
        # para os mais antigos, podemos parar.
        if oldest_clip < cutoff:
            break

        cursor = data.get(
            "pagination",
            {}
        ).get("cursor")

        if not cursor:
            break

    return all_clips, cutoff


# ============================================================
# DISCORD
# ============================================================

def send_to_discord(clip):

    if not DISCORD_WEBHOOK:

        raise RuntimeError(
            "DISCORD_CLIP_WEBHOOK_URL não configurado."
        )

    title = clip.get(
        "title",
        "Novo clip"
    )

    creator = clip.get(
        "creator_name",
        "Desconhecido"
    )

    views = clip.get(
        "view_count",
        0
    )

    clip_url = clip.get(
        "url"
    )

    created_at = clip.get(
        "created_at"
    )

    payload = {

        "username": "JOTTA Clip Watcher",

        "content": "🎬 **NOVO CLIP DO JOTTA**",

        "embeds": [
            {

                "title": title,

                "url": clip_url,

                "description":
                    f"📺 **Streamer:** {CHANNEL_LOGIN}\n"
                    f"✂️ **Criado por:** {creator}\n"
                    f"👁️ **Visualizações:** {views}\n"
                    f"🕐 **Publicado:** {created_at}",

                "footer": {
                    "text": "JOTTA Clip Watcher"
                }

            }
        ]
    }

    for attempt in range(
        1,
        MAX_DISCORD_RETRIES + 1
    ):

        response = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=30
        )

        # Discord aceitou
        if response.status_code in (200, 204):

            return True

        # Rate limit
        if response.status_code == 429:

            retry_after = None

            try:

                body = response.json()

                retry_after = body.get(
                    "retry_after"
                )

            except Exception:
                pass

            if retry_after is None:

                retry_header = response.headers.get(
                    "Retry-After"
                )

                if retry_header:

                    try:
                        retry_after = float(
                            retry_header
                        )
                    except ValueError:
                        pass

            if retry_after is None:
                retry_after = 2 ** attempt

            retry_after = float(
                retry_after
            ) + 0.5

            print(
                f"⚠️ Discord rate limit."
                f" Tentativa {attempt}/{MAX_DISCORD_RETRIES}."
            )

            print(
                f"⏳ A aguardar "
                f"{retry_after:.1f} segundos..."
            )

            time.sleep(
                retry_after
            )

            continue

        # Erros temporários do Discord
        if response.status_code >= 500:

            wait_time = min(
                2 ** attempt,
                30
            )

            print(
                f"⚠️ Discord HTTP "
                f"{response.status_code}."
            )

            print(
                f"⏳ A aguardar "
                f"{wait_time} segundos..."
            )

            time.sleep(
                wait_time
            )

            continue

        # Outros erros
        raise RuntimeError(
            f"Discord webhook erro: "
            f"HTTP {response.status_code} "
            f"{response.text}"
        )

    raise RuntimeError(
        "Discord continuou a aplicar rate limit "
        "depois de várias tentativas."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("🎬 JOTTA TWITCH CLIP WATCHER")
    print("=" * 60)

    if not CLIENT_ID:
        raise RuntimeError(
            "TWITCH_CLIENT_ID não configurado."
        )

    if not CLIENT_SECRET:
        raise RuntimeError(
            "TWITCH_CLIENT_SECRET não configurado."
        )

    if not DISCORD_WEBHOOK:
        raise RuntimeError(
            "DISCORD_CLIP_WEBHOOK_URL não configurado."
        )

    now = utc_now()

    print(
        f"🕐 Hora actual UTC: "
        f"{now.isoformat()}"
    )

    print(
        f"🔎 A procurar clips publicados "
        f"nos últimos {LOOKBACK_MINUTES} minutos."
    )

    # --------------------------------------------------------
    # Estado
    # --------------------------------------------------------

    state = load_state()

    seen_clips = set(
        state.get(
            "seen_clips",
            []
        )
    )

    # --------------------------------------------------------
    # Twitch
    # --------------------------------------------------------

    token = get_access_token()

    broadcaster_id = get_broadcaster_id(
        token
    )

    print(
        f"🆔 Broadcaster ID: "
        f"{broadcaster_id}"
    )

    clips, cutoff = get_clips(
        token,
        broadcaster_id
    )

    print(
        f"🔎 Clips recebidos da Twitch: "
        f"{len(clips)}"
    )

    print(
        f"⏰ Limite temporal: "
        f"{cutoff.isoformat()}"
    )

    # --------------------------------------------------------
    # Filtrar clips
    # --------------------------------------------------------

    recent_clips = []

    for clip in clips:

        clip_id = clip["id"]

        created_at = parse_twitch_time(
            clip["created_at"]
        )

        age_seconds = (
            now - created_at
        ).total_seconds()

        age_minutes = (
            age_seconds / 60
        )

        print(
            f"🎞️ {clip.get('title', 'Sem título')} "
            f"| publicado há "
            f"{age_minutes:.1f} min"
        )

        # Clip demasiado antigo
        if created_at < cutoff:

            continue

        # Já enviado anteriormente
        if clip_id in seen_clips:

            print(
                "   ↳ ⏭️ Já enviado anteriormente."
            )

            continue

        # Clip novo
        recent_clips.append(
            clip
        )

    # Mais antigos primeiro
    recent_clips.sort(
        key=lambda clip:
        parse_twitch_time(
            clip["created_at"]
        )
    )

    print(
        f"🆕 Clips novos nos últimos "
        f"{LOOKBACK_MINUTES} minutos: "
        f"{len(recent_clips)}"
    )

    # --------------------------------------------------------
    # Enviar
    # --------------------------------------------------------

    sent = 0

    for clip in recent_clips:

        clip_id = clip["id"]

        print(
            f"📨 A enviar clip "
            f"{clip_id} para Discord..."
        )

        try:

            send_to_discord(
                clip
            )

            # Só guardamos depois do Discord
            # confirmar que recebeu.
            seen_clips.add(
                clip_id
            )

            sent += 1

            print(
                "   ✅ Enviado com sucesso."
            )

            # Pequena pausa entre mensagens.
            time.sleep(1)

        except Exception as error:

            print(
                f"   ❌ Erro ao enviar: "
                f"{error}"
            )

            # IMPORTANTE:
            # Não adicionamos o ID ao seen_clips.
            #
            # Assim, se falhar, tenta novamente
            # na próxima execução.

    # --------------------------------------------------------
    # Guardar estado
    # --------------------------------------------------------

    state["seen_clips"] = list(
        seen_clips
    )

    # Limite para o ficheiro não crescer
    # indefinidamente.
    if len(
        state["seen_clips"]
    ) > 5000:

        state["seen_clips"] = (
            state["seen_clips"][-5000:]
        )

    save_state(
        state
    )

    # --------------------------------------------------------
    # Resultado
    # --------------------------------------------------------

    print("=" * 60)

    print(
        f"📊 Clips analisados: "
        f"{len(clips)}"
    )

    print(
        f"🆕 Clips novos: "
        f"{len(recent_clips)}"
    )

    print(
        f"📨 Enviados para Discord: "
        f"{sent}"
    )

    print(
        f"🧠 Clips guardados na base: "
        f"{len(state['seen_clips'])}"
    )

    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        print("=" * 60)
        print("❌ CHECK FALHOU")
        print("=" * 60)

        print(
            f"Erro: {error}"
        )

        sys.exit(1)

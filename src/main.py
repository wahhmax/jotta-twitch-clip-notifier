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
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


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

    except Exception as error:

        print(
            f"⚠️ Erro ao ler state.json: {error}"
        )

        return {
            "seen_clips": []
        }


def save_state(state):

    os.makedirs(
        "data",
        exist_ok=True
    )

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


# ============================================================
# DISCORD
# ============================================================

def send_interval_message(
    start_time,
    end_time,
    clip_count
):

    if not DISCORD_WEBHOOK:

        raise RuntimeError(
            "DISCORD_CLIP_WEBHOOK_URL "
            "não está configurado."
        )

    payload = {

        "username":
            "JOTTA Clip Watcher",

        "content":
            (
                "🕐 **JOTTA Clip Watcher**\n\n"

                f"Clips entre "
                f"**{start_time.strftime('%H:%M:%S')}** "
                f"e "
                f"**{end_time.strftime('%H:%M:%S')} UTC**\n\n"

                f"🎯 **{clip_count} "
                f"clips novos detetados**"
            )
    }

    print(
        "📨 A enviar mensagem de intervalo para Discord..."
    )

    for attempt in range(
        1,
        6
    ):

        response = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=30
        )

        print(
            f"📡 Discord intervalo: "
            f"HTTP {response.status_code}"
        )

        if response.status_code in (
            200,
            204
        ):

            print(
                "✅ Mensagem de intervalo enviada."
            )

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

                retry_after = 2 ** attempt

            retry_after = (
                float(retry_after)
                + 0.5
            )

            print(
                "⚠️ Discord rate limit no intervalo."
            )

            print(
                f"⏳ A aguardar "
                f"{retry_after:.1f}s..."
            )

            time.sleep(
                retry_after
            )

            continue

        # Erro temporário
        if response.status_code >= 500:

            wait = min(
                2 ** attempt,
                30
            )

            print(
                f"⚠️ Discord HTTP "
                f"{response.status_code}"
            )

            print(
                f"⏳ A aguardar {wait}s..."
            )

            time.sleep(
                wait
            )

            continue

        print(
            f"❌ Discord respondeu: "
            f"{response.text}"
        )

        return False

    print(
        "❌ Não foi possível enviar "
        "a mensagem de intervalo."
    )

    return False


def send_to_discord(
    clip
):

    if not DISCORD_WEBHOOK:

        raise RuntimeError(
            "DISCORD_CLIP_WEBHOOK_URL "
            "não está configurado."
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

    url = clip.get(
        "url"
    )

    created_at = clip.get(
        "created_at"
    )

    payload = {

        "username":
            "JOTTA Clip Watcher",

        "content":
            "🎬 **NOVO CLIP DO JOTTA**",

        "embeds": [

            {

                "title":
                    title,

                "url":
                    url,

                "description":
                    (
                        f"📺 **Streamer:** "
                        f"{CHANNEL}\n\n"

                        f"✂️ **Criado por:** "
                        f"{creator}\n\n"

                        f"👁️ **Visualizações:** "
                        f"{views}\n\n"

                        f"🕐 **Publicado:** "
                        f"{created_at}"
                    ),

                "footer": {

                    "text":
                        "JOTTA Clip Watcher"
                }
            }
        ]
    }

    print(
        "📨 A enviar clip para Discord..."
    )

    for attempt in range(
        1,
        6
    ):

        response = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=30
        )

        print(
            f"📡 Discord: "
            f"HTTP {response.status_code}"
        )

        if response.status_code in (
            200,
            204
        ):

            print(
                "✅ Discord recebeu o clip."
            )

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

                retry_after = 2 ** attempt

            retry_after = (
                float(retry_after)
                + 0.5
            )

            print(
                "⚠️ Discord rate limit."
            )

            print(
                f"⏳ A aguardar "
                f"{retry_after:.1f}s..."
            )

            time.sleep(
                retry_after
            )

            continue

        # Erro temporário
        if response.status_code >= 500:

            wait = min(
                2 ** attempt,
                30
            )

            print(
                f"⚠️ Discord HTTP "
                f"{response.status_code}"
            )

            print(
                f"⏳ A aguardar {wait}s..."
            )

            time.sleep(
                wait
            )

            continue

        print(
            f"❌ Discord respondeu: "
            f"{response.text}"
        )

        return False

    print(
        "❌ Discord continuou a rejeitar "
        "o pedido depois de várias tentativas."
    )

    return False


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)

    print(
        "🎬 JOTTA TWITCH CLIP WATCHER"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Validar secrets
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Tempo
    # --------------------------------------------------------

    now = now_utc()

    recent_cutoff = (
        now
        - timedelta(
            minutes=RECENT_WINDOW_MINUTES
        )
    )

    print(
        f"🕐 AGORA UTC: "
        f"{format_time(now)}"
    )

    print(
        f"🔎 Pesquisa Twitch: "
        f"últimas {SEARCH_WINDOW_HOURS} horas"
    )

    print(
        f"🎯 Novo clip: últimos "
        f"{RECENT_WINDOW_MINUTES} minutos"
    )

    # --------------------------------------------------------
    # Estado
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Twitch
    # --------------------------------------------------------

    token = get_access_token()

    channel_id = get_channel_id(
        token
    )

    print(
        f"🆔 JOTTA ID: "
        f"{channel_id}"
    )

    clips = get_last_24h_clips(
        token,
        channel_id
    )

    print(
        f"📦 Total de clips encontrados "
        f"nas últimas 24h: {len(clips)}"
    )

    # --------------------------------------------------------
    # Filtrar
    # --------------------------------------------------------

    new_clips = []

    print("-" * 70)

    for clip in clips:

        clip_id = clip["id"]

        created_at = parse_time(
            clip["created_at"]
        )

        age_minutes = (
            now - created_at
        ).total_seconds() / 60

        print(
            f"🎞️ {clip.get('title', 'Sem título')}"
        )

        print(
            f"   ID: {clip_id}"
        )

        print(
            f"   Criado: "
            f"{format_time(created_at)}"
        )

        print(
            f"   Idade: "
            f"{age_minutes:.2f} minutos"
        )

        # Já enviado
        if clip_id in seen:

            print(
                "   ⏭️ JÁ ENVIADO"
            )

            print("-" * 70)

            continue

        # Futuro, por alguma diferença de relógio
        if created_at > now:

            print(
                "   ⚠️ Data futura. Ignorado."
            )

            print("-" * 70)

            continue

        # Mais antigo que a janela recente
        if created_at < recent_cutoff:

            print(
                "   ⏭️ MAIS ANTIGO QUE "
                f"{RECENT_WINDOW_MINUTES} MINUTOS"
            )

            print("-" * 70)

            continue

        # Novo
        print(
            "   🟢 NOVO CLIP DETETADO!"
        )

        new_clips.append(
            clip
        )

        print("-" * 70)

    # Mais antigos primeiro
    new_clips.sort(
        key=lambda clip:
        parse_time(
            clip["created_at"]
        )
    )

    print(
        f"🆕 Clips novos nos últimos "
        f"{RECENT_WINDOW_MINUTES} minutos: "
        f"{len(new_clips)}"
    )

    # --------------------------------------------------------
    # Mensagem de intervalo
    # --------------------------------------------------------

    send_interval_message(
        recent_cutoff,
        now,
        len(new_clips)
    )

    # --------------------------------------------------------
    # Enviar
    # --------------------------------------------------------

    sent = 0

    for clip in new_clips:

        clip_id = clip["id"]

        print(
            f"📨 Clip: {clip_id}"
        )

        success = send_to_discord(
            clip
        )

        if success:

            seen.add(
                clip_id
            )

            sent += 1

            print(
                "💾 ID guardado."
            )

        else:

            print(
                "❌ ID NÃO guardado."
            )

            print(
                "🔁 Será tentado novamente "
                "na próxima execução."
            )

        # Evitar pedidos seguidos
        time.sleep(1)

    # --------------------------------------------------------
    # Guardar estado
    # --------------------------------------------------------

    state["seen_clips"] = list(
        seen
    )

    # Limitar tamanho
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

    print("=" * 70)

    print(
        f"📊 Clips encontrados nas últimas 24h: "
        f"{len(clips)}"
    )

    print(
        f"🎯 Clips dentro dos últimos "
        f"{RECENT_WINDOW_MINUTES} min: "
        f"{len(new_clips)}"
    )

    print(
        f"📨 Enviados para Discord: "
        f"{sent}"
    )

    print(
        f"🧠 Total de IDs guardados: "
        f"{len(seen)}"
    )

    print("=" * 70)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        print("=" * 70)

        print(
            "❌ CHECK FALHOU"
        )

        print("=" * 70)

        print(
            f"Erro: {error}"
        )

        sys.exit(1)

from __future__ import annotations

from typing import Iterable

DEFAULT_UNIDADE = "DEST"

CHANNEL_OPTIONS = [
    "Instagram",
    "WhatsApp",
    "E-mail",
    "LinkedIn",
    "Site",
]

DEFAULT_CHANNELS = ["Instagram", "Site"]

# Fallback lists. You can replace these with your full departmental rosters.
DOCENTES_DEST = [
    "Adriano Silva",
    "Aline Souza",
    "Bruna Oliveira",
    "Carlos Santos",
    "Fernanda Costa",
    "Joao Pereira",
    "Luiza Almeida",
    "Maria Santos",
    "Ricardo Rocha",
]

DOCENTES_DMAT = [
    "Ana Paula",
    "Bruno Matos",
    "Camila Nunes",
    "Diego Lima",
    "Felipe Andrade",
    "Helena Cardoso",
    "Paulo Roberto",
    "Rafaela Dias",
]

DEMAND_TYPES = {
    "Docente": [
        "Divulgacao de evento",
        "Divulgacao de edital",
        "Divulgacao de defesa",
        "Divulgacao de conquista",
        "Outro",
    ],
    "Chefe de Departamento": [
        "Comunicado institucional",
        "Divulgacao oficial",
        "Chamada interna",
        "Outro",
    ],
    "Colegiado": [
        "Comunicado academico",
        "Calendario/cronograma",
        "Divulgacao de selecao",
        "Outro",
    ],
    "Representante do Nucleo de Extensao": [
        "Divulgacao de projeto",
        "Divulgacao de curso",
        "Divulgacao de evento",
        "Outro",
    ],
    "Coordenador de Laboratorio": [
        "Divulgacao de laboratorio",
        "Divulgacao de oportunidade",
        "Divulgacao de evento",
        "Outro",
    ],
    "Direcao do Instituto": [
        "Comunicado institucional",
        "Divulgacao oficial",
        "Outro",
    ],
    "CEAPG": ["Comunicado", "Divulgacao de atividade", "Outro"],
    "CEAD": ["Comunicado", "Divulgacao de atividade", "Outro"],
    "CEAG": ["Comunicado", "Divulgacao de atividade", "Outro"],
    "Secretaria Executiva": ["Comunicado", "Aviso", "Outro"],
    "Coordenador do NEX": ["Comunicado", "Planejamento", "Outro"],
    "Membro da Equipe": ["Divulgacao de acao", "Atualizacao de projeto", "Outro"],
    "Outro": ["Solicitacao geral"],
}


def _normalize_channel_name(channel: str) -> str:
    channel = str(channel or "").strip()
    if not channel:
        return ""
    lower = channel.lower()
    aliases = {
        "instagram": "Instagram",
        "insta": "Instagram",
        "whatsapp": "WhatsApp",
        "zap": "WhatsApp",
        "email": "E-mail",
        "e-mail": "E-mail",
        "linkedin": "LinkedIn",
        "site": "Site",
        "portal": "Site",
    }
    return aliases.get(lower, channel)


def normalize_channels(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    else:
        raw_items = list(value)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        channel = _normalize_channel_name(item)
        if not channel:
            continue
        if channel in seen:
            continue
        seen.add(channel)
        normalized.append(channel)
    return normalized


def channels_label(value: Iterable[str] | str | None) -> str:
    channels = normalize_channels(value)
    return ", ".join(channels) if channels else "Nao informado"


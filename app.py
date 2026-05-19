# -*- coding: utf-8 -*-
import asyncio
import io
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app_core.config import load_config
from app_core.domain import (
    CHANNEL_OPTIONS,
    DEFAULT_CHANNELS,
    DEFAULT_UNIDADE,
    DEMAND_TYPES,
    DOCENTES_DEST,
    DOCENTES_DMAT,
    channels_label,
    normalize_channels,
)
from app_core.emailer import EmailNotifier, SubmissionEmailPayload
from app_core.firebase import FirebaseClient
from app_core.http import HttpClient
from app_core.prompts import PROMPT_SISTEMA_TEXTO, PROMPT_SISTEMA_VISUAL

st.set_page_config(page_title="Comunica IME", layout="centered")

CONFIG = load_config(st)
HTTP = HttpClient()
FIREBASE = FirebaseClient(CONFIG, HTTP)
EMAIL_NOTIFIER = EmailNotifier(CONFIG)
GEMINI_API_KEY_SECRET = CONFIG.gemini_api_key

COORDENADOR_PROJETO_EMAIL = "ricardo8610@gmail.com"
ADMIN_EMAILS = ["ricardo8610@gmail.com"]
ESTAGIARIOS_EMAILS = ["estagiariosime@gmail.com"]
RESPONSAVEL_NEX_NAO_DEFINIDO = "N\u00e3o definido ainda"
RESPONSAVEIS_NEX_OPCOES = [
    "Coordenação",
    "Nathalie",
    "Luana",
    "Caroline",
    "Duda",
]
STATUS_PENDENTE = "Pendente"
STATUS_EM_PRODUCAO_ARTES = "Em produ\u00e7\u00e3o das artes"
STATUS_CONCLUIDO = "Conclu\u00eddo"
STATUS_APROVADO_ENVIADO = "Aprovado e enviado"
STATUS_ARQUIVADO = "Arquivado"


def upload_to_storage(file_bytes, file_name, mime_type):
    try:
        return FIREBASE.upload_to_storage(file_bytes, file_name, mime_type)
    except Exception as e:
        st.error(f"Falha técnica no upload: {str(e)}")
        return None


@st.cache_data(ttl=30, show_spinner=False)
def _listar_documentos_cache(colecao):
    return FIREBASE.list_documents(colecao)


def invalidar_cache_documentos():
    _listar_documentos_cache.clear()


def atualizar_documento(colecao, doc_id, campos):
    ok = FIREBASE.update_fields(colecao, doc_id, campos)
    if ok:
        invalidar_cache_documentos()
    return ok


def adicionar_documento(colecao, dados):
    try:
        resultado = FIREBASE.add_document(colecao, dados)
        if resultado[0]:
            invalidar_cache_documentos()
        return resultado
    except Exception as e:
        return False, str(e)


def listar_documentos(colecao):
    documentos = _listar_documentos_cache(colecao)
    documentos_normalizados = []
    for item in documentos:
        item = dict(item)
        if "solicitando_como" not in item and item.get("postando_como"):
            item["solicitando_como"] = item["postando_como"]
        if "canais" in item:
            item["canais"] = normalize_channels(item["canais"])
        if colecao == "solicitacoes" and not item.get("responsavel_nex"):
            item["responsavel_nex"] = RESPONSAVEL_NEX_NAO_DEFINIDO
        documentos_normalizados.append(item)
    return documentos_normalizados


def atualizar_status_solicitacao(doc_id, novo_status):
    return atualizar_documento("solicitacoes", doc_id, {"status": novo_status})


def atualizar_tentativas_ia(doc_id, novo_array):
    try:
        return atualizar_documento("solicitacoes", doc_id, {"tentativas_ia": novo_array})
    except Exception as e:
        print(f"Exception ao atualizar tentativas_ia: {e}")
        return False


def email_contato_valido(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()))


def formatar_data_para_email(data_val) -> str:
    if pd.isna(data_val) or not data_val:
        return "Não informado"
    if isinstance(data_val, datetime):
        return data_val.strftime("%d/%m/%Y %H:%M")
    return str(data_val)


def formatar_data_para_email(data_val) -> str:
    if data_val is None:
        return "Nao informado"
    if isinstance(data_val, str) and not data_val.strip():
        return "Nao informado"
    texto = str(data_val).strip()
    if not texto or texto.lower() in {"nan", "nat", "none"}:
        return "Nao informado"
    if isinstance(data_val, datetime):
        return data_val.strftime("%d/%m/%Y %H:%M")
    return texto


def formatar_links_email(urls: list[str] | None) -> str:
    links = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    if not links:
        return "Sem anexos."
    return "\n".join(f"- {u}" for u in links)


def montar_links_markdown(urls: list[str] | None) -> str:
    links = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    if not links:
        return "Sem anexos."
    
    items = []
    for idx, url in enumerate(links, start=1):
        # Tentar extrair a extensão do arquivo na URL
        try:
            path = url.split('?')[0]  # Remove tokens/parâmetros do Firebase
            ext = path.split('.')[-1].upper()
            if len(ext) > 5 or '/' in ext: # Sanitização básica
                ext = "ARQUIVO"
            items.append(f"- [Anexo {idx} - {ext}]({url})")
        except:
            items.append(f"- [Anexo {idx}]({url})")
            
    return "\n".join(items)


def normalizar_lista_emails(emails) -> list[str]:
    if emails is None:
        return []
    if isinstance(emails, str):
        emails = [emails]

    normalizados = []
    vistos = set()
    for email in emails:
        email_limpo = str(email or "").strip()
        if not email_limpo or email_limpo in vistos:
            continue
        vistos.add(email_limpo)
        normalizados.append(email_limpo)
    return normalizados


def obter_tipo_demanda_email(solicitacao: dict) -> str:
    return (
        solicitacao.get("tipo")
        or solicitacao.get("tipo_evento")
        or "Nao informado"
    )


def obter_previsao_email(solicitacao: dict) -> str:
    data_publicacao = solicitacao.get("data_publicacao")
    if data_publicacao:
        return formatar_data_para_email(data_publicacao)

    periodo_inicio = solicitacao.get("periodo_inicio")
    periodo_fim = solicitacao.get("periodo_fim")
    if periodo_inicio or periodo_fim:
        inicio = formatar_data_para_email(periodo_inicio)
        fim = formatar_data_para_email(periodo_fim)
        return f"Inicio: {inicio} | Fim: {fim}"

    return "Nao informado"


def mostrar_feedback_envio_email(
    erros: list[str] | None,
    mensagem_ok: str = "📧 E-mails enviados.",
    writer=None,
) -> bool:
    erros_validos = [erro for erro in (erros or []) if erro]
    if erros_validos:
        for erro in erros_validos:
            if writer:
                writer(f"[Aviso] {erro}")
            else:
                st.warning(erro)
        return False

    if CONFIG.email_enabled:
        if writer:
            writer(mensagem_ok)
        else:
            st.info(mensagem_ok)
        time.sleep(1)
    return True


def enviar_email_personalizado(
    solicitacao: dict,
    destino_email,
    tipo_email: str,
    descricao_email: str,
    canais_override: Optional[list[str]] = None,
    bcc_emails: Optional[list[str]] = None,
    audience: str = "interno",
    intro_email: str = "",
    closing_email: str = "",
) -> list[str]:
    try:
        destinos = normalizar_lista_emails(destino_email)
        copias_ocultas = normalizar_lista_emails(bcc_emails)
        if not destinos and not copias_ocultas:
            return ["E-mail de destino n\u00e3o informado."]
        canais = (
            normalize_channels(canais_override)
            if canais_override is not None
            else normalize_channels(solicitacao.get("canais", []))
        )
        payload = SubmissionEmailPayload(
            solicitante=solicitacao.get("solicitante", "Solicitante"),
            email=destinos[0] if destinos else "",
            unidade=solicitacao.get("unidade", ""),
            solicitando_como=solicitacao.get("solicitando_como", ""),
            tipo=tipo_email,
            canais=canais,
            descricao=descricao_email,
            data_publicacao=obter_previsao_email(solicitacao),
            urgencia=bool(solicitacao.get("urgencia", False)),
            audience=audience,
            intro=intro_email,
            closing=closing_email,
        )
        return EMAIL_NOTIFIER.send_email(
            payload,
            to_emails=destinos,
            bcc_emails=copias_ocultas,
        )
    except Exception as e:
        return [f"Erro ao enviar e-mail: {e}"]


def notificar_inicio_producao(solicitacao: dict, responsavel_nex: str) -> list[str]:
    # Para eventos, o campo é tipo_evento, para criativos é tipo
    tipo_demanda = solicitacao.get('tipo') or solicitacao.get('tipo_evento') or 'Não informado'
    detalhes = (
        "Atualizacao de status da solicitacao\n"
        f"- Novo status: {STATUS_EM_PRODUCAO_ARTES}\n"
        f"- Responsavel NEX: {responsavel_nex}\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Não informado')}\n"
    )
    erros = []
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            solicitacao.get("email", ""),
            "Atualizacao da sua solicitacao",
            detalhes,
        )
    )
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            COORDENADOR_PROJETO_EMAIL,
            "Demanda entrou em producao de artes",
            detalhes,
        )
    )
    return [e for e in erros if e]


def notificar_conclusao_para_coordenador(
    solicitacao: dict, resposta_texto: str, resposta_anexos: list[str]
) -> list[str]:
    detalhes = (
        "Demanda marcada como concluida para aprovacao final\n"
        f"- Novo status: {STATUS_CONCLUIDO}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n"
        f"- Tipo da demanda: {solicitacao.get('tipo', 'Nao informado')}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n\n"
        "Resposta de producao\n"
        f"{resposta_texto.strip() if resposta_texto and resposta_texto.strip() else 'Sem texto.'}\n\n"
        f"Anexos:\n{formatar_links_email(resposta_anexos)}\n"
    )
    return enviar_email_personalizado(
        solicitacao,
        COORDENADOR_PROJETO_EMAIL,
        "Demanda concluida e aguardando aprovacao final",
        detalhes,
    )


def notificar_aprovacao_final_para_solicitante(
    solicitacao: dict, resposta_texto: str, resposta_anexos: list[str], parecer: str
) -> list[str]:
    detalhes = (
        "Sua solicitacao foi aprovada e os conteudos estao liberados.\n"
        f"- Status: {STATUS_APROVADO_ENVIADO}\n"
        f"- Tipo da demanda: {solicitacao.get('tipo', 'Nao informado')}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n\n"
        "Conteudos gerados\n"
        f"{resposta_texto.strip() if resposta_texto and resposta_texto.strip() else 'Sem texto.'}\n\n"
        f"Anexos:\n{formatar_links_email(resposta_anexos)}\n\n"
        f"Parecer final do coordenador:\n{parecer.strip() if parecer and parecer.strip() else 'Aprovado sem observacoes adicionais.'}\n"
    )
    return enviar_email_personalizado(
        solicitacao,
        solicitacao.get("email", ""),
        "Aprovacao final da sua solicitacao",
        detalhes,
    )


def notificar_retorno_para_producao(solicitacao: dict, parecer: str) -> list[str]:
    detalhes = (
        "A demanda retornou para producao de artes.\n"
        f"- Novo status: {STATUS_EM_PRODUCAO_ARTES}\n"
        f"- Tipo da demanda: {solicitacao.get('tipo', 'Nao informado')}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n\n"
        f"Parecer do coordenador:\n{parecer.strip() if parecer and parecer.strip() else 'Sem observacoes.'}\n"
    )
    return enviar_email_personalizado(
        solicitacao,
        COORDENADOR_PROJETO_EMAIL,
        "Demanda retornou para producao",
        detalhes,
    )


def notificar_nova_solicitacao(solicitacao: dict) -> list[str]:
    tipo_demanda = obter_tipo_demanda_email(solicitacao)
    descricao_original = str(solicitacao.get("descricao") or "").strip() or "Sem descricao."
    detalhes_internos = (
        "Nova solicitacao recebida para triagem.\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n"
        f"- Solicitando como: {solicitacao.get('solicitando_como', 'Nao informado')}\n"
        f"- Previsao: {obter_previsao_email(solicitacao)}\n\n"
        "Descricao informada:\n"
        f"{descricao_original}"
    )
    detalhes_solicitante = (
        "Recebemos sua solicitacao com sucesso e ela entrou na fila de triagem.\n\n"
        "Resumo informado:\n"
        f"{descricao_original}"
    )

    erros = []
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            solicitacao.get("email", ""),
            "Confirmacao da sua solicitacao",
            detalhes_solicitante,
            audience="solicitante",
            intro_email="Recebemos sua solicitacao no Comunica IME.",
            closing_email="Voce recebera novas atualizacoes por e-mail ao longo do fluxo.",
        )
    )
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            ADMIN_EMAILS,
            "Nova solicitacao recebida",
            detalhes_internos,
            bcc_emails=ESTAGIARIOS_EMAILS,
            audience="interno",
            intro_email="Uma nova solicitacao foi registrada na plataforma.",
            closing_email="A demanda ja esta disponivel para triagem no painel.",
        )
    )
    return [e for e in erros if e]


def notificar_inicio_producao(solicitacao: dict, responsavel_nex: str) -> list[str]:
    tipo_demanda = obter_tipo_demanda_email(solicitacao)
    detalhes = (
        "Atualizacao de status da solicitacao\n"
        f"- Novo status: {STATUS_EM_PRODUCAO_ARTES}\n"
        f"- Responsavel NEX: {responsavel_nex}\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n"
    )
    erros = []
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            solicitacao.get("email", ""),
            "Atualizacao da sua solicitacao",
            detalhes,
            audience="solicitante",
            intro_email="Sua solicitacao saiu da fila pendente e entrou em producao.",
            closing_email="A equipe segue trabalhando no material e voce sera avisado sobre os proximos passos.",
        )
    )
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            ADMIN_EMAILS,
            "Demanda entrou em producao de artes",
            detalhes,
            bcc_emails=ESTAGIARIOS_EMAILS,
            audience="interno",
            intro_email="A demanda mudou de pendente para em producao.",
            closing_email="Acompanhe a execucao pelo painel.",
        )
    )
    return [e for e in erros if e]


def notificar_conclusao_para_coordenador(
    solicitacao: dict, resposta_texto: str, resposta_anexos: list[str]
) -> list[str]:
    tipo_demanda = obter_tipo_demanda_email(solicitacao)
    detalhes = (
        "Demanda marcada como concluida para aprovacao final\n"
        f"- Novo status: {STATUS_CONCLUIDO}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n\n"
        "Resposta de producao\n"
        f"{resposta_texto.strip() if resposta_texto and resposta_texto.strip() else 'Sem texto.'}\n\n"
        f"Anexos:\n{formatar_links_email(resposta_anexos)}\n"
    )
    return enviar_email_personalizado(
        solicitacao,
        ADMIN_EMAILS,
        "Demanda concluida e aguardando aprovacao final",
        detalhes,
        audience="interno",
        intro_email="Uma demanda da fila em andamento recebeu resposta e esta aguardando conferencia final.",
        closing_email="Revise o conteudo e decida se a solicitacao deve ser aprovada ou retornar para producao.",
    )


def notificar_aprovacao_final_para_solicitante(
    solicitacao: dict, resposta_texto: str, resposta_anexos: list[str], parecer: str
) -> list[str]:
    tipo_demanda = obter_tipo_demanda_email(solicitacao)
    detalhes = (
        "Sua solicitacao foi aprovada e os conteudos estao liberados.\n"
        f"- Status: {STATUS_APROVADO_ENVIADO}\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n\n"
        "Conteudos gerados\n"
        f"{resposta_texto.strip() if resposta_texto and resposta_texto.strip() else 'Sem texto.'}\n\n"
        f"Anexos:\n{formatar_links_email(resposta_anexos)}\n\n"
        f"Parecer final do coordenador:\n{parecer.strip() if parecer and parecer.strip() else 'Aprovado sem observacoes adicionais.'}\n"
    )
    detalhes_estagiarios = (
        "A solicitacao foi aprovada na fila para conferir e saiu como finalizada.\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n"
    )

    erros = []
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            solicitacao.get("email", ""),
            "Aprovacao final da sua solicitacao",
            detalhes,
            audience="solicitante",
            intro_email="Sua solicitacao foi aprovada e o conteudo gerado esta disponivel abaixo.",
            closing_email="Se precisar de um novo ciclo, basta abrir outra solicitacao na plataforma.",
        )
    )
    erros.extend(
        enviar_email_personalizado(
            solicitacao,
            ESTAGIARIOS_EMAILS,
            "Solicitacao finalizada com sucesso",
            detalhes_estagiarios,
            audience="interno",
            intro_email="Uma solicitacao da fila para conferir foi aprovada.",
            closing_email="Nao ha nova acao operacional pendente para esta demanda.",
        )
    )
    return [e for e in erros if e]


def notificar_retorno_para_producao(solicitacao: dict, parecer: str) -> list[str]:
    tipo_demanda = obter_tipo_demanda_email(solicitacao)
    detalhes = (
        "A demanda retornou para producao de artes.\n"
        f"- Novo status: {STATUS_EM_PRODUCAO_ARTES}\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Solicitante: {solicitacao.get('solicitante', 'Nao informado')}\n"
        f"- Responsavel NEX: {solicitacao.get('responsavel_nex', RESPONSAVEL_NEX_NAO_DEFINIDO)}\n\n"
        f"Parecer do coordenador:\n{parecer.strip() if parecer and parecer.strip() else 'Sem observacoes.'}\n"
    )
    return enviar_email_personalizado(
        solicitacao,
        ESTAGIARIOS_EMAILS,
        "Demanda retornou para producao",
        detalhes,
        audience="interno",
        intro_email="A solicitacao nao recebeu OK final e voltou para a fila em andamento.",
        closing_email="Revise o parecer e retome a producao.",
    )


def render_links_markdown(urls: list[str] | None, titulo: str):
    markdown_links = montar_links_markdown(urls)
    if markdown_links == "Sem anexos.":
        return
    st.markdown(f"**{titulo}**")
    st.markdown(markdown_links)


def normalizar_status(status: str) -> str:
    texto = unicodedata.normalize("NFKD", str(status or ""))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    return texto.strip().lower()


def status_eh_pendente(status: str) -> bool:
    return normalizar_status(status) in {"", "pendente"}


def status_eh_em_producao(status: str) -> bool:
    return normalizar_status(status) == normalizar_status(STATUS_EM_PRODUCAO_ARTES)


def status_eh_concluido(status: str) -> bool:
    return normalizar_status(status) == normalizar_status(STATUS_CONCLUIDO)


def status_eh_arquivado(status: str) -> bool:
    return normalizar_status(status) == normalizar_status(STATUS_ARQUIVADO)


def montar_rotulo_solicitacao(sol: dict) -> str:
    doc_id = (sol.get("id") or "sem-id")[:8]
    tipo = sol.get("tipo", "Sem tipo")
    solicitante = sol.get("solicitante", "Sem solicitante")
    status = sol.get("status", STATUS_PENDENTE)
    return f"{tipo} | {solicitante} | {status} | id:{doc_id}"


def build_email_template_criativos(
    tipo_demanda: str,
    canais: list[str],
    data_publicacao: datetime,
    descricao: str,
    urgencia: bool,
) -> str:
    canais_txt = ", ".join(canais) if canais else "Nao informado"
    urgencia_txt = "SIM" if urgencia else "NAO"
    descricao_txt = descricao.strip() if descricao and descricao.strip() else "Nao informado"
    return (
        "RESUMO DA SOLICITACAO DE CRIATIVOS E DIVULGACAO\n"
        f"- Tipo da demanda: {tipo_demanda}\n"
        f"- Canais de divulgacao: {canais_txt}\n"
        f"- Data/hora pretendida: {data_publicacao.strftime('%d/%m/%Y %H:%M')}\n"
        f"- Urgencia: {urgencia_txt}\n\n"
        "DETALHAMENTO\n"
        f"{descricao_txt}\n"
    )


def build_email_template_apoio_tecnico(
    tipo_evento: str,
    local_evento: str,
    periodo_inicio: datetime,
    periodo_fim: datetime,
    apoios_necessarios: list[str],
    descricao: str,
    urgencia: bool,
) -> str:
    apoios_txt = ", ".join(apoios_necessarios) if apoios_necessarios else "Nao informado"
    urgencia_txt = "SIM" if urgencia else "NAO"
    descricao_txt = descricao.strip() if descricao and descricao.strip() else "Nao informado"
    return (
        "RESUMO DA SOLICITACAO DE APOIO TECNICO\n"
        f"- Tipo de evento: {tipo_evento}\n"
        f"- Local do evento: {local_evento}\n"
        f"- Inicio pretendido: {periodo_inicio.strftime('%d/%m/%Y %H:%M')}\n"
        f"- Fim pretendido: {periodo_fim.strftime('%d/%m/%Y %H:%M')}\n"
        f"- Apoios necessarios: {apoios_txt}\n"
        f"- Urgencia: {urgencia_txt}\n\n"
        "DETALHAMENTO\n"
        f"{descricao_txt}\n"
    )


def render_header_banner():
    st.markdown(
        """
        <style>
        #header-banner-anchor + div[data-testid="stImage"] img {
            border: 1px solid #0b66c3;
        }
        </style>
        <div id="header-banner-anchor"></div>
        """,
        unsafe_allow_html=True,
    )
    st.image("assets/banner.png", use_container_width=True)


def render_persistent_footer():
    st.markdown(
        """
        <style>
        .persistent-footer {
            margin-top: 200px;
            padding-top: 14px;
            border-top: 1px solid #d8dee9;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="persistent-footer">', unsafe_allow_html=True)
    _, col_center, _ = st.columns([1, 8, 1])
    with col_center:
        st.image("assets/barra_de_logos.png", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


def page_solicitar_publicacao():
    unidade_options = ["DEST", "DMAT", "IME", "NEX"]

    st.markdown("### 🎨 Solicitação de Criativos e Divulgação")

    unidade = st.pills(
        "Unidade",
        unidade_options,
        selection_mode="single",
        default=DEFAULT_UNIDADE,
        width="stretch",
        key="criativos_unidade_pills",
    )
    
    # Lógica de opções por unidade
    opcoes_solicitando = []
    if unidade in ("DEST", "DMAT"):
        opcoes_solicitando = ["Docente", "Colegiado", "Departamento", "Pós-graduação"]
    elif unidade == "IME":
        opcoes_solicitando = ["Diretor", "Coordenador", "Docente"]
    elif unidade == "NEX":
        opcoes_solicitando = ["Docente", "Coordenador", "Estagiário"]

    colA, colB, colC = st.columns(3)
    with colA:
        solicitante = st.text_input(
            "Solicitante",
            placeholder="Digite o nome do solicitante...",
            disabled=not bool(unidade),
        ).strip()
    with colB:
        solicitando_como = st.selectbox(
            "Solicitando como:",
            opcoes_solicitando,
            index=None,
            placeholder="Selecione o cargo/papel...",
            disabled=not bool(unidade),
        )
    with colC:
        email_contato = st.text_input(
            "E-mail de contato",
            placeholder="nome@ufba.br",
            help="Este e-mail receberá a confirmação da solicitação e será usado para contato sobre a demanda.",
        ).strip()
    
    TIPOS_SOLICITACAO = [
        "Divulgação de evento",
        "Divulgação de edital ou seleção",
        "Divulgação de defesa (TCC, Mestrado, Doutorado)",
        "Divulgação de curso ou oficina",
        "Comunicado institucional",
        "Campanha ou ação de engajamento",
        "Produção de banner ou cartaz",
        "Newsletter ou e-mail marketing",
        "Atualização de conteúdo no site",
        "Divulgação de conquista ou resultado",
        "Convite digital ou folder",
        "Criação de identidade visual básica",
        "Outro",
    ]
    col_tipo, col_data = st.columns(2)
    with col_tipo:
        tipo_demanda = st.selectbox(
            "Tipo de Solicitação",
            TIPOS_SOLICITACAO,
            index=None,
            placeholder="Selecione o tipo...",
        )
    with col_data:
        # Validação de Data de Publicação (Mínimo 24h, Urgência < 48h)
        default_date = (datetime.now() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
        data_pub = st.datetime_input(
            "Data e Hora pretendida de publicação",
            value=default_date,
            step=timedelta(hours=1),
            format="DD/MM/YYYY",
            help="O prazo mínimo é de 24h. Entre 24h e 48h é considerado URGENTE."
        )
    
    agora = datetime.now()
    diferenca = data_pub - agora
    urgencia = False
    data_valida = True

    if diferenca < timedelta(hours=24):
        st.error("⚠️ O prazo mínimo para solicitações é de **24 horas**. Por favor, escolha um horário posterior.")
        data_valida = False
    elif diferenca < timedelta(hours=48):
        st.warning("🚨 **Atenção**: Esta solicitação será tratada com **URGÊNCIA** (Prazo menor que 48h).")
        urgencia = True

    canais = st.segmented_control(
        "Canais de Divulgação",
        CHANNEL_OPTIONS,
        default=["Instagram"],
        selection_mode="multi",
        width="stretch",
        key="criativos_canais_segmented",
    )

    st.markdown("### 📎 Detalhes da Solicitação")
    st.caption("Utilize o campo abaixo para descrever o conteúdo e anexar materiais. Depois, confirme o envio no botão abaixo.")

    # CSS para aumentar a altura do campo de texto do chat_input
    st.markdown(
        """
        <style>
        [data-testid="stChatInput"] textarea {
            min-height: 120px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    email_valido = email_contato_valido(email_contato)
    formulario_pronto = bool(
        unidade and solicitante and solicitando_como and email_valido and data_valida
    )
    chat_placeholder = (
        "⚠️ Preencha Unidade, Solicitante, Solicitando como, E-mail válido e Data/Hora válida para liberar o campo."
        if not formulario_pronto
        else "Descreva detalhadamente a sua solicitação (URLs, horários, locais, objetivos, público-alvo). Use o ícone de anexo para incluir arquivos."
    )

    with st.container():
        solicitacao_input = st.chat_input(
            chat_placeholder,
            accept_file="multiple",
            disabled=not formulario_pronto,
        )

    # Armazena rascunho no session_state ao enviar pelo chat_input
    if solicitacao_input:
        st.session_state["rascunho_solicitacao"] = {
            "texto": solicitacao_input.text or "",
            "arquivos": solicitacao_input.files or [],
        }

    rascunho = st.session_state.get("rascunho_solicitacao")
    if rascunho:
        with st.container(border=True):
            st.markdown("#### 📋 Resumo da Solicitação")
            col_prev1, col_prev2 = st.columns([2, 1])
            with col_prev1:
                st.markdown("**Descrição:**")
                st.text(rascunho["texto"] if rascunho["texto"] else "Sem descrição.")
            with col_prev2:
                st.markdown(f"**Unidade:** {unidade or '-'}")
                st.markdown(f"**Solicitante:** {solicitante or '-'}")
                st.markdown(f"**Tipo:** {tipo_demanda or '-'}")
                n_anexos = len(rascunho.get("arquivos") or [])
                st.markdown(f"**Anexos:** {n_anexos} arquivo(s)")
            st.caption("Para alterar a descrição ou os anexos, envie novamente pelo campo acima.")

        pode_submeter = formulario_pronto and tipo_demanda
        enviar_confirmado = st.button(
            "🚀 Confirmar e Submeter Solicitação",
            type="primary",
            use_container_width=True,
            disabled=not pode_submeter,
        )

        if enviar_confirmado:
            erros_validacao = []
            if not unidade:
                erros_validacao.append("Selecione a unidade.")
            if not solicitante:
                erros_validacao.append("Informe o nome do solicitante.")
            if not solicitando_como:
                erros_validacao.append("Selecione o campo 'Solicitando como'.")
            if not email_valido:
                erros_validacao.append("Informe um e-mail de contato válido.")
            if not data_valida:
                erros_validacao.append("Ajuste a data/hora de publicação para um prazo mínimo de 24 horas.")
            if not tipo_demanda:
                erros_validacao.append("Selecione o tipo de solicitação.")

            if erros_validacao:
                for erro in erros_validacao:
                    st.error(f"⚠️ {erro}")
                render_persistent_footer()
                return

            with st.status("\U0001F680 Registrando sua solicita\u00e7\u00e3o e subindo anexos...", expanded=True) as status:
                descricao_final = rascunho["texto"]
                arquivos = rascunho.get("arquivos") or []
                
                links_final = []
                
                # Processamento de Arquivos Selecionados
                for f in arquivos:
                    status.write(f"\U0001F4E4 Subindo arquivo: {f.name}...")
                    url_storage = upload_to_storage(f.getvalue(), f.name, f.type)
                    if url_storage:
                        links_final.append(url_storage)
                
                # Persistência no Firestore
                dados_solicitacao = {
                    "unidade": unidade,
                    "solicitante": solicitante,
                    "email": email_contato,
                    "solicitando_como": solicitando_como,
                    "tipo": tipo_demanda,
                    "data_publicacao": data_pub,
                    "canais": canais,
                    "descricao": descricao_final,
                    "anexos": links_final,
                    "data_solicitacao": datetime.now(),
                    "status": STATUS_PENDENTE,
                    "responsavel_nex": RESPONSAVEL_NEX_NAO_DEFINIDO,
                    "urgencia": urgencia
                }
                
                sucesso, msg_erro = adicionar_documento("solicitacoes", dados_solicitacao)
                if sucesso:
                    status.write("📧 Enviando notificações por e-mail...")
                    payload_email = SubmissionEmailPayload(
                        solicitante=solicitante,
                        email=email_contato,
                        unidade=unidade,
                        solicitando_como=solicitando_como,
                        tipo=tipo_demanda,
                        canais=canais,
                        descricao=descricao_final,
                        data_publicacao=data_pub.strftime("%d/%m/%Y %H:%M"),
                        urgencia=urgencia,
                    )
                    
                    # Definimos os destinatários (Admin + Estagiários) como BCC (além do To: solicitante)
                    
                    erros_email = notificar_nova_solicitacao(dados_solicitacao)
                    mostrar_feedback_envio_email(
                        erros_email,
                        mensagem_ok="📧 E-mails de confirmacao enviados.",
                        writer=status.write,
                    )
                    st.session_state.pop("rascunho_solicitacao", None)
                    status.update(label="\u2705 Solicita\u00e7\u00e3o registrada com sucesso!", state="complete", expanded=False)
                    st.balloons()
                    msg_urg = " (Tratada como URGENTE)" if urgencia else ""
                    nome_exibicao = solicitante.split(" ")[0] if solicitante else "Solicitante"
                    st.success(f"\U0001F4BB Tudo pronto, {nome_exibicao}! Sua demanda{msg_urg} foi enviada.")
                    time.sleep(2.5)
                    st.rerun()
                else:
                    status.update(label=f"❌ Erro ao salvar no banco de dados: {msg_erro}", state="error")
    render_persistent_footer()


def page_solicitar_apoio_eventos_transmissoes():
    unidade_options = ["DEST", "DMAT", "IME", "NEX"]

    st.markdown("### \U0001F3A5 Solicita\u00e7\u00e3o de Apoio T\u00e9cnico a Eventos e Transmiss\u00f5es")

    unidade = st.pills(
        "Unidade",
        unidade_options,
        selection_mode="single",
        default=DEFAULT_UNIDADE if DEFAULT_UNIDADE in unidade_options else "DEST",
        width="stretch",
        key="eventos_unidade_pills",
    )

    opcoes_local_evento = ["Auditório Maria Zezé", "Sala 148", "Auditório do PAF", "Lab 143", "Outro"]
    if hasattr(st, "pills"):
        local_evento = st.pills(
            "Local do evento",
            opcoes_local_evento,
            selection_mode="single",
            width="stretch",
            key="evento_local_pills",
        )
    else:
        local_evento = st.segmented_control(
            "Local do evento",
            opcoes_local_evento,
            selection_mode="single",
            width="stretch",
            key="evento_local_fallback",
        )

    # Lógica de opções por unidade (mesmo padrão de Criativos)
    opcoes_solicitando = []
    if unidade in ("DEST", "DMAT"):
        opcoes_solicitando = ["Docente", "Colegiado", "Departamento", "Pós-graduação"]
    elif unidade == "IME":
        opcoes_solicitando = ["Diretor", "Coordenador", "Docente"]
    elif unidade == "NEX":
        opcoes_solicitando = ["Docente", "Coordenador", "Estagiário"]

    colA, colB, colC = st.columns(3)
    with colA:
        solicitante = st.text_input(
            "Solicitante",
            placeholder="Digite o nome do solicitante...",
            disabled=not bool(unidade),
            key="evento_solicitante",
        ).strip()
    with colB:
        solicitando_como = st.selectbox(
            "Solicitando como:",
            opcoes_solicitando,
            index=None,
            placeholder="Selecione o cargo/papel...",
            disabled=not bool(unidade),
            key="evento_solicitando_como",
        )
    with colC:
        email_contato = st.text_input(
            "E-mail de contato",
            placeholder="nome@ufba.br",
            help="Este e-mail receberá a confirmação da solicitação e será usado para contato sobre a demanda.",
            key="evento_email_contato",
        ).strip()

    tipos_evento = [
        "Palestra",
        "Seminário",
        "Defesa de TCC",
        "Defesa de Mestrado/Doutorado",
        "Minicurso / Oficina",
        "Colóquio",
        "Aula Magna",
        "Evento Institucional",
        "Live / Webinário",
        "Transmissão ao Vivo",
        "Outro",
    ]

    col_tipo, col_inicio, col_fim = st.columns(3)
    with col_tipo:
        tipo_evento = st.selectbox(
            "Tipo de Evento",
            tipos_evento,
            index=None,
            placeholder="Selecione o tipo...",
            disabled=not bool(solicitando_como),
            key="evento_tipo_evento",
        )
    with col_inicio:
        default_inicio = (datetime.now() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
        periodo_inicio = st.datetime_input(
            "Início pretendido",
            value=default_inicio,
            step=timedelta(hours=1),
            key="evento_periodo_inicio",
        )
    with col_fim:
        default_fim = (datetime.now() + timedelta(days=7, hours=2)).replace(minute=0, second=0, microsecond=0)
        periodo_fim = st.datetime_input(
            "Fim pretendido",
            value=default_fim,
            step=timedelta(hours=1),
            key="evento_periodo_fim",
        )

    agora = datetime.now()
    periodo_valido = True
    urgencia = False
    if periodo_inicio < agora + timedelta(hours=24):
        st.error("⚠️ O prazo mínimo para solicitações é de 24 horas de antecedência para o início.")
        periodo_valido = False
    elif periodo_inicio < agora + timedelta(hours=48):
        st.warning("🚨 Atenção: esta solicitação será tratada com URGÊNCIA (início em menos de 48h).")
        urgencia = True

    if periodo_fim <= periodo_inicio:
        st.error("⚠️ O fim do período deve ser posterior ao início.")
        periodo_valido = False

    opcoes_apoio = [
        "Projetor",
        "Notebook/Computador",
        "Microfone de mão",
        "Microfone de lapela",
        "Caixa de som",
        "Mesa de som",
        "Ponteiro/Passador de slides",
        "TV/Monitor de apoio",
        "Cabo HDMI/Adaptadores",
        "Iluminação",
        "Gravação de vídeo",
        "Fotografia do evento",
        "Transmissão ao vivo (YouTube/Meet)",
        "Operador técnico",
        "Suporte de internet/Wi-Fi",
        "Outro apoio técnico",
    ]
    if hasattr(st, "pills"):
        apoios_necessarios = st.pills(
            "Apoios necessários",
            opcoes_apoio,
            selection_mode="multi",
            width="stretch",
            key="evento_apoios",
        )
    else:
        apoios_necessarios = st.segmented_control(
            "Apoios necessários",
            opcoes_apoio,
            selection_mode="multi",
            width="stretch",
            key="evento_apoios_fallback",
        )
    if not apoios_necessarios:
        apoios_necessarios = []

    st.markdown("### 📎 Detalhes da Solicitação")
    st.caption("Utilize o campo abaixo para descrever o evento/transmissão e anexar materiais. Depois, confirme o envio no botão abaixo.")

    # CSS para aumentar a altura do campo de texto do chat_input
    st.markdown(
        """
        <style>
        [data-testid="stChatInput"] textarea {
            min-height: 120px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    email_valido = email_contato_valido(email_contato)
    formulario_pronto = bool(
        unidade and local_evento and solicitante and solicitando_como and tipo_evento and email_valido and periodo_valido
    )

    chat_placeholder = (
        "⚠️ Preencha Unidade, Local do evento, Solicitante, Solicitando como, Tipo de Evento, E-mail válido e Período válido para liberar o campo."
        if not formulario_pronto
        else "Descreva detalhadamente a solicitação (objetivo, local/plataforma, público estimado, necessidades técnicas, links e responsáveis). Use o ícone de anexo para incluir arquivos."
    )

    with st.container():
        solicitacao_input = st.chat_input(
            chat_placeholder,
            accept_file="multiple",
            disabled=not formulario_pronto,
            key="chat_input_eventos",
        )

    # Armazena rascunho no session_state ao enviar pelo chat_input
    if solicitacao_input:
        st.session_state["rascunho_eventos"] = {
            "texto": solicitacao_input.text or "",
            "arquivos": solicitacao_input.files or [],
        }

    rascunho = st.session_state.get("rascunho_eventos")
    if rascunho:
        with st.container(border=True):
            st.markdown("#### 📋 Resumo da Solicitação de Apoio Técnico")
            col_prev1, col_prev2 = st.columns([2, 1])
            with col_prev1:
                st.markdown("**Descrição:**")
                st.text(rascunho["texto"] if rascunho["texto"] else "Sem descrição.")
            with col_prev2:
                st.markdown(f"**Unidade:** {unidade or '-'}")
                st.markdown(f"**Local:** {local_evento or '-'}")
                st.markdown(f"**Tipo:** {tipo_evento or '-'}")
                n_anexos = len(rascunho.get("arquivos") or [])
                st.markdown(f"**Anexos:** {n_anexos} arquivo(s)")
            st.caption("Para alterar a descrição ou os anexos, envie novamente pelo campo acima.")

        pode_submeter = formulario_pronto and tipo_evento
        enviar_confirmado = st.button(
            "🚀 Confirmar e Submeter Solicitação Técnica",
            type="primary",
            use_container_width=True,
            disabled=not pode_submeter,
            key="btn_confirmar_eventos"
        )

        if enviar_confirmado:
            erros_validacao = []
            if not unidade:
                erros_validacao.append("Selecione a unidade.")
            if not local_evento:
                erros_validacao.append("Selecione o local do evento.")
            if not solicitante:
                erros_validacao.append("Informe o nome do solicitante.")
            if not solicitando_como:
                erros_validacao.append("Selecione o campo 'Solicitando como'.")
            if not email_valido:
                erros_validacao.append("Informe um e-mail de contato válido.")
            if not periodo_valido:
                erros_validacao.append("Ajuste o período para um prazo mínimo de 24 horas.")
            if not tipo_evento:
                erros_validacao.append("Selecione o tipo de evento.")

            if erros_validacao:
                for erro in erros_validacao:
                    st.error(f"⚠️ {erro}")
                render_persistent_footer()
                return

            with st.status("🚀 Registrando sua solicitação e subindo anexos...", expanded=True) as status:
                descricao_final = rascunho["texto"]
                arquivos = rascunho.get("arquivos") or []
                links_final = []

                for f in arquivos:
                    status.write(f"📤 Subindo arquivo: {f.name}...")
                    url_storage = upload_to_storage(f.getvalue(), f.name, f.type)
                    if url_storage:
                        links_final.append(url_storage)

                dados_solicitacao = {
                    "unidade": unidade,
                    "local_evento": local_evento,
                    "solicitante": solicitante,
                    "email": email_contato,
                    "solicitando_como": solicitando_como,
                    "tipo_evento": tipo_evento,
                    "periodo_inicio": periodo_inicio,
                    "periodo_fim": periodo_fim,
                    "apoios_necessarios": apoios_necessarios,
                    "descricao": descricao_final,
                    "anexos": links_final,
                    "data_solicitacao": datetime.now(),
                    "status": STATUS_PENDENTE,
                    "urgencia": urgencia,
                }

                sucesso, msg_error = adicionar_documento("solicitacoes_eventos_transmissoes", dados_solicitacao)
                if sucesso:
                    status.write("📧 Enviando notificações por e-mail...")
                    payload_email = SubmissionEmailPayload(
                        solicitante=solicitante,
                        email=email_contato,
                        unidade=unidade,
                        solicitando_como=solicitando_como,
                        tipo=f"Apoio a Evento/Transmissão: {tipo_evento}",
                        canais=[],
                        descricao=descricao_final,
                        data_publicacao=f"Início: {periodo_inicio.strftime('%d/%m/%Y %H:%M')} | Fim: {periodo_fim.strftime('%d/%m/%Y %H:%M')}",
                        urgencia=urgencia,
                    )
                    
                    # Destinatários (Admin + Estagiários)
                    erros_email = notificar_nova_solicitacao(dados_solicitacao)
                    mostrar_feedback_envio_email(
                        erros_email,
                        mensagem_ok="📧 E-mails de confirmacao enviados.",
                        writer=status.write,
                    )
                    st.session_state.pop("rascunho_eventos", None)
                    status.update(label="✅ Solicitação registrada com sucesso!", state="complete", expanded=False)
                    st.balloons()
                    msg_urg = " (Tratada como URGENTE)" if urgencia else ""
                    nome_exibicao = solicitante.split(" ")[0] if solicitante else "Solicitante"
                    st.success(f"💻 Tudo pronto, {nome_exibicao}! Sua demanda{msg_urg} foi enviada.")
                    time.sleep(2.5)
                    st.rerun()
                else:
                    status.update(label=f"❌ Erro ao salvar no banco de dados: {msg_error}", state="error")

    render_persistent_footer()


def page_dashboard_solicitacoes():
    st.markdown(
        """
        <style>
        .block-container {
            max-width: 1200px !important;
            padding-top: 2rem;
        }
        </style>
        """,
        unsafe_allow_html=True
    )
    render_header_banner()
    
    solicitacoes_todas = listar_documentos("solicitacoes")
    
    # Filtra apenas solicitações com status "Em produção das artes" (Em andamento)
    solicitacoes = [
        s for s in solicitacoes_todas 
        if normalizar_status(s.get("status")) == normalizar_status(STATUS_EM_PRODUCAO_ARTES)
    ]
    
    if not solicitacoes:
        st.info("Nenhuma solicitação em andamento encontrada.")
        render_persistent_footer()
        return

    # Ordenação: Data de publicação mais próxima primeiro (ascendente)
    solicitacoes.sort(key=lambda x: x.get("data_publicacao", "9999-12-31"))

    def formatar_br(data_val):
        if not data_val: return "N/A"
        try:
            # Caso já seja um objeto datetime (raro via REST, mas possível localmente)
            if isinstance(data_val, datetime):
                return data_val.strftime("%d/%m/%Y %H:%M")
            
            # Limpeza de string para ISO (remove 'T', 'Z', etc)
            ds = str(data_val).replace("T", " ").replace("Z", "").split(".")[0]
            
            # Tenta converter de formatos comuns
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ds, fmt)
                    return dt.strftime("%d/%m/%Y %H:%M")
                except:
                    continue
            return ds # Fallback se nada funcionar
        except:
            return str(data_val)

    def extrair_nome_arquivo(url):
        """Tenta extrair o nome real do arquivo da URL do Firebase Storage."""
        try:
            import urllib.parse
            path_part = url.split('/o/')[-1].split('?')[0]
            decoded_path = urllib.parse.unquote(path_part)
            return decoded_path.split('/')[-1]
        except:
            return "Arquivo"

    def carregar_templates_card():
        template_dir = os.path.join("assets", "templates-cards")
        templates_list = ["Nenhum"]
        if os.path.exists(template_dir):
            templates_list += [
                f for f in os.listdir(template_dir)
                if f.lower().endswith((".png", ".jpg", ".jpeg"))
            ]
        return template_dir, templates_list

    def nome_template_amigavel(template_name: str) -> str:
        if template_name == "Nenhum":
            return "Nenhum"
        return os.path.splitext(template_name)[0].replace('_', ' ').strip().title()

    IMAGE_GENERATION_TIMEOUT_SECONDS = 300

    def render_opcoes_proposta(tentativa: dict, tentativa_idx: int, prefixo: str):
        opcoes = tentativa.get("opcoes", [])
        if not opcoes:
            st.info("Nenhuma opção salva nesta tentativa.")
            return

        textos_por_opcao = []
        for op in opcoes:
            try:
                textos_por_opcao.append(json.loads(op.get("legenda", "{}")))
            except:
                textos_por_opcao.append({})

        mapping = [
            ("instagram", "Instagram"),
            ("whatsapp", "WhatsApp"),
            ("email", "E-mail"),
            ("linkedin", "LinkedIn"),
            ("site", "Site"),
        ]
        available_channels = [m for m in mapping if any(txts.get(m[0]) for txts in textos_por_opcao)]
        cols = st.columns(len(opcoes))
        for op_idx, op in enumerate(opcoes):
            with cols[op_idx]:
                st.markdown(f"#### Opção {op.get('id_opcao', op_idx + 1)}")
                if op.get("imagem_url"):
                    st.image(op["imagem_url"], use_container_width=True)
                else:
                    st.info("Sem arte gerada para esta opção.")

                if available_channels:
                    abas = st.tabs([label for _, label in available_channels])
                    for aba_idx, (key, label) in enumerate(available_channels):
                        with abas[aba_idx]:
                            val = textos_por_opcao[op_idx].get(key, "")
                            if not val:
                                st.caption(f"Sem proposta para {label}.")
                                continue

                            st.text_area(
                                f"Texto {label}",
                                value=val,
                                height=260,
                                key=f"{prefixo}_{tentativa_idx}_{op_idx}_{key}",
                                label_visibility="collapsed",
                            )

                            if key == "site":
                                if st.button("Publicar no site", key=f"pub_{prefixo}_{tentativa_idx}_{op_idx}"):
                                    linhas = val.strip().split('\n')
                                    titulo = next((l for l in linhas if l.strip()), "Notícia do Departamento")
                                    titulo = titulo.replace('*', '').replace('#', '').strip()
                                    doc_site = {
                                        "titulo": titulo,
                                        "conteudo": val,
                                        "autor": "Comunicação IME",
                                        "data": datetime.utcnow(),
                                        "tipo": "noticia",
                                        "imagem_url": op.get("imagem_url"),
                                    }
                                    s, m = adicionar_documento("conteudos", doc_site)
                                    if s:
                                        st.success("Postado!")
                                    else:
                                        st.error(m)
                else:
                    st.caption("Nenhum texto retornado para esta opção.")

    opcoes_radio = []
    dict_sols = {}
    for sol in solicitacoes:
        status = sol.get("status", STATUS_PENDENTE)
        urgente = sol.get("urgencia", False)
        header_prefix = "🚨 " if urgente and status_eh_pendente(status) else ""
        if status_eh_pendente(status):
            status_icon = "⏳"
        elif status_eh_em_producao(status):
            status_icon = "🎨"
        elif (
            status_eh_concluido(status)
            or normalizar_status(status) == normalizar_status(STATUS_APROVADO_ENVIADO)
            or normalizar_status(status) == "feito"
        ):
            status_icon = "✅"
        else:
            status_icon = "❌"
        
        tipo = sol.get('tipo', 'Solicitação').upper()
        responsavel = sol.get("responsavel_nex") or RESPONSAVEL_NEX_NAO_DEFINIDO
        
        titulo_opcao = f"{status_icon} {header_prefix}{responsavel} - {tipo}"
        opcoes_radio.append(titulo_opcao)
        dict_sols[titulo_opcao] = sol
        
    col_escolha, col_contexto_topo = st.columns([1, 2], gap="medium")
    with col_escolha:
        with st.container(border=True):
            st.subheader("🎯 Escolha a proposta para trabalhar")
            selecionado = st.selectbox("Selecione um pedido", opcoes_radio, label_visibility="collapsed")

    with col_contexto_topo:
        with st.container(border=True):
            st.subheader("📋 Ver contexto da solicitação")
            if selecionado:
                sol_preview = dict_sols[selecionado]
                anexos = sol_preview.get("anexos", [])
                if isinstance(anexos, str):
                    anexos = [anexos]

                col_materiais, col_info = st.columns([1, 3])

                with col_info:
                    st.write(f"**Tipo:** {sol_preview.get('tipo', 'Solicitação')}")
                    if sol_preview.get("descricao"):
                        st.info(sol_preview.get("descricao"))
                    else:
                        st.caption("Sem descrição informada.")

                with col_materiais:
                    st.write("**Materiais de referência**")
                    if anexos:
                        for link in anexos:
                            l_lower = str(link).lower()
                            nome_arq = extrair_nome_arquivo(str(link))
                            if any(ext in l_lower for ext in [".png", ".jpg", ".jpeg", ".webp"]) or ("alt=media" in l_lower and not any(a in l_lower for a in [".wav", ".mp3", ".pdf"])):
                                st.image(link, use_container_width=True)
                                st.markdown(f"[Abrir {nome_arq}]({link})")
                            elif any(ext in l_lower for ext in [".wav", ".mp3", ".ogg"]):
                                st.audio(link)
                                st.markdown(f"[Abrir {nome_arq}]({link})")
                            elif ".pdf" in l_lower:
                                st.markdown(f"📄 [{nome_arq}]({link})")
                            else:
                                st.markdown(f"🔗 [{nome_arq}]({link})")
                    else:
                        st.caption("Esta solicitação não tem anexos de apoio.")
            else:
                st.caption("Selecione uma proposta para visualizar o contexto.")

    st.divider()

    if selecionado:
        sol = dict_sols[selecionado]

        st.subheader("🤖 Gerador de conteúdo da proposta")

        with st.container(border=True):
            instrucoes_ia = st.text_area(
                "Direção criativa e instruções extras",
                placeholder="Ex: linguagem mais direta, chamada mais forte, arte com destaque para data e local, versão mais institucional.",
                key=f"ins_ia_{sol['id']}",
                height=220,
            )

            template_dir, templates_list = carregar_templates_card()

            if len(templates_list) > 1:
                st.markdown("#### Templates disponíveis")
                preview_templates = templates_list[1:6]
                cols = st.columns(6)
                with cols[0]:
                    template_selecionado = st.radio(
                        "Template-base da arte",
                        options=templates_list,
                        key=f"tpl_ia_{sol['id']}",
                        format_func=nome_template_amigavel,
                    )

                for idx in range(5):
                    with cols[idx + 1]:
                        if idx < len(preview_templates):
                            t_file = preview_templates[idx]
                            st.image(os.path.join(template_dir, t_file), use_container_width=True)
                            st.caption(nome_template_amigavel(t_file))
                        else:
                            st.empty()
            else:
                template_selecionado = st.radio(
                    "Template-base da arte",
                    options=templates_list,
                    key=f"tpl_ia_{sol['id']}",
                    format_func=nome_template_amigavel,
                )

            if st.button("Gerar 3 opções de proposta", key=f"ia_btn_{sol['id']}", use_container_width=True, type="primary"):
                if GEMINI_API_KEY_SECRET:
                    client = genai.Client(api_key=GEMINI_API_KEY_SECRET)
                        
                    async def perform_generation():
                            # Containers para as 3 colunas
                            cols = st.columns(3)
                            
                            # Preparar placeholders para status e timers em cada coluna
                            p_status = [cols[i].empty() for i in range(3)]
                            p_timers = [cols[i].empty() for i in range(3)]
                            p_progress = [cols[i].empty() for i in range(3)]

                            try:
                                # 1. Preparar Conteúdo Multimodal (Upload único para economizar tempo)
                                for p in p_status: p.info("📥 Preparando arquivos...")
                                anexos_parts = []
                                anexos_solicitacao = sol.get("anexos", [])
                                if isinstance(anexos_solicitacao, str): anexos_solicitacao = [anexos_solicitacao]

                                for link in anexos_solicitacao:
                                    nome_arq = extrair_nome_arquivo(link)
                                    try:
                                        resp_file = HTTP.get(link)
                                        if resp_file.status_code == 200:
                                            ext = os.path.splitext(nome_arq)[1].lower() or ".bin"
                                            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
                                                tmp_file.write(resp_file.content)
                                                tmp_path = tmp_file.name
                                            gemini_file = client.files.upload(file=tmp_path)
                                            anexos_parts.append(gemini_file)
                                            os.remove(tmp_path)
                                    except: pass
                                
                                # Adiciona o template selecionado se houver
                                template_part = None
                                if template_selecionado != "Nenhum":
                                    path_tpl = os.path.join("assets", "templates-cards", template_selecionado)
                                    if os.path.exists(path_tpl):
                                        template_part = client.files.upload(file=path_tpl)

                                class TextosGerados(BaseModel):
                                    instagram: Optional[str] = Field(None)
                                    whatsapp: Optional[str] = Field(None)
                                    email: Optional[str] = Field(None)
                                    linkedin: Optional[str] = Field(None)
                                    site: Optional[str] = Field(None)

                                # 2. Worker Assíncrono com monitoramento de estado interno
                                async def gerar_proposta_individual_async(id_opcao, p_stat, p_img_placeholder):
                                    try:
                                        p_stat.markdown(f"**Opção {id_opcao}**: Iniciando...")
                                        
                                        # Geração de Texto
                                        p_stat.markdown(f"**Opção {id_opcao}**: Gerando texto...")
                                        prompt_texto = f"""
                                        {PROMPT_SISTEMA_TEXTO}
                                        ---
                                        DEMANDA DO USUÁRIO: {sol.get('descricao')}
                                        CANAIS SOLICITADOS: {sol.get('canais')}
                                        INSTRUÇÕES ESPECIAFICAS DESTA SOLICITAÇÃO: {instrucoes_ia}
                                        """
                                        
                                        task_texto = client.aio.models.generate_content(
                                            model="gemini-3.1-flash-lite-preview", 
                                            contents=anexos_parts + [prompt_texto],
                                            config=types.GenerateContentConfig(
                                                response_mime_type="application/json",
                                                response_schema=TextosGerados,
                                            ),
                                        )
                                        
                                        # Geração de Imagem
                                        p_stat.markdown(f"**Opção {id_opcao}**: Gerando imagem...")
                                        anexos_img = list(anexos_parts)
                                        if template_part: 
                                            anexos_img.append(template_part)
                                            # Se tem template, reforçamos no prompt
                                            template_note = "USE O TEMPLATE ANEXADO (Template de Card) COMO BASE DE DESIGN."
                                        else:
                                            template_note = ""

                                        prompt_img = f"""
                                        {PROMPT_SISTEMA_VISUAL}
                                        ---
                                        {template_note}
                                        CONTEÚDO DA IMAGEM BASEADO EM: {sol.get('descricao')}
                                        NOTAS EXTRAS DO USUÁRIO: {instrucoes_ia}
                                        FORMATO OBRIGATÓRIO: Post Redes Sociais 4:5.
                                        """
                                        
                                        task_imagem = client.aio.models.generate_content(
                                            model="gemini-3-pro-image-preview",
                                            contents=anexos_img + [prompt_img + ' (image output only)'],
                                            config=types.GenerateContentConfig(
                                                response_modalities=['IMAGE'],
                                                image_config=types.ImageConfig(aspect_ratio="4:5")
                                            )
                                        )

                                        resp_text, resp_img = await asyncio.gather(
                                            task_texto,
                                            asyncio.wait_for(task_imagem, timeout=IMAGE_GENERATION_TIMEOUT_SECONDS),
                                        )
                                        
                                        # Processar Imagem
                                        img_bytes = None
                                        for part in resp_img.parts:
                                            if part.inline_data: img_bytes = part.inline_data.data; break
                                            elif hasattr(part, "as_image"):
                                                b_arr = io.BytesIO()
                                                part.as_image().save(b_arr, format='PNG')
                                                img_bytes = b_arr.getvalue()
                                                break
                                        
                                        if img_bytes:
                                            p_img_placeholder.image(img_bytes, caption=f"Preview Opção {id_opcao}", use_container_width=True)
                                        
                                        p_stat.success(f"Opção {id_opcao}: Pronta!")
                                        return {"textos": json.loads(resp_text.text), "imagem_bytes": img_bytes, "status": "sucesso"}
                                    except asyncio.TimeoutError:
                                        p_stat.error(f"Opção {id_opcao}: tempo limite de 300s na geração da imagem.")
                                        return {"status": "erro", "erro": "Timeout de 300s na geração da imagem"}
                                    except Exception as e:
                                        p_stat.error(f"Erro na Opção {id_opcao}: {str(e)}")
                                        return {"status": "erro", "erro": str(e)}

                                # 3. Execução e Timer em Tempo Real
                                for p in p_status: p.info("🚀 Iniciando Propostas...")
                                tasks = [
                                    asyncio.create_task(gerar_proposta_individual_async(1, p_status[0], p_progress[0])),
                                    asyncio.create_task(gerar_proposta_individual_async(2, p_status[1], p_progress[1])),
                                    asyncio.create_task(gerar_proposta_individual_async(3, p_status[2], p_progress[2]))
                                ]

                                start_time = time.perf_counter()
                                while any(not t.done() for t in tasks):
                                    for i, t in enumerate(tasks):
                                        if not t.done():
                                            elapsed = time.perf_counter() - start_time
                                            p_timers[i].markdown(f"⏱️ **{elapsed:.1f}s**")
                                    await asyncio.sleep(0.1)

                                # Parada final dos timers
                                for i, t in enumerate(tasks):
                                    elapsed = time.perf_counter() - start_time
                                    if t.done():
                                        p_timers[i].markdown(f"🏁 Finalizado em **{elapsed:.1f}s**")

                                resultados = [await t for t in tasks]

                                # 4. Agrupar Resultados e Persistir
                                opcoes_sucesso = []
                                for idx, res in enumerate(resultados):
                                    if res["status"] == "sucesso":
                                        img_url = None
                                        if res["imagem_bytes"]:
                                            nome_f = f"ia_op_{sol['id']}_{int(time.time())}_{idx}.png"
                                            img_url = upload_to_storage(res["imagem_bytes"], nome_f, "image/png")
                                        
                                        opcoes_sucesso.append({
                                            "legenda": json.dumps(res["textos"]),
                                            "imagem_url": img_url,
                                            "id_opcao": idx + 1
                                        })
                                
                                if opcoes_sucesso:
                                    tentativas_atuais = sol.get("tentativas_ia", [])
                                    tentativas_atuais.append({
                                        "data": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                                        "opcoes": opcoes_sucesso,
                                        "instrucoes_usadas": instrucoes_ia
                                    })
                                    atualizar_tentativas_ia(sol['id'], tentativas_atuais)
                                    st.toast("✅ Propostas salvas no histórico!", icon="🎉")
                                    time.sleep(1) # Delay curto para o usuário ver o "Concluído" nas colunas
                                    st.rerun()

                            except Exception as e_geral:
                                st.error(f"Erro Crítico: {e_geral}")

                    asyncio.run(perform_generation())

        tentativas_ia_banco = sol.get("tentativas_ia", [])
        if tentativas_ia_banco:
            st.markdown("---")
            st.markdown("### Histórico de propostas")
            st.caption("Cada tentativa mostra 3 colunas, uma para cada proposta gerada.")

            lista_tents = []
            for t in tentativas_ia_banco:
                if isinstance(t, dict):
                    lista_tents.append(t)
                elif isinstance(t, str):
                    try:
                        lista_tents.append(json.loads(t))
                    except:
                        pass

            for idx, t in enumerate(reversed(lista_tents)):
                num = len(lista_tents) - idx
                label_tent = f"Tentativa {num} • {t.get('data', '')}"
                with st.expander(label_tent, expanded=(idx == 0)):
                    if t.get("instrucoes_usadas"):
                        st.markdown("**Instruções usadas nesta rodada**")
                        st.info(t["instrucoes_usadas"])

                    if "opcoes" in t and isinstance(t["opcoes"], list):
                        render_opcoes_proposta(t, num, "txt_his")
                    else:
                        c1, c2 = st.columns([1, 1.2])
                        with c1:
                            if t.get("imagem_url"):
                                st.image(t["imagem_url"], use_container_width=True)
                        with c2:
                            try:
                                textos = json.loads(t.get("legenda", "{}"))
                                st.json(textos)
                            except:
                                st.text_area(f"Legenda {num}", t.get("legenda", ""), height=200)
    render_persistent_footer()

def page_adicionar_noticia():
    render_header_banner()
    st.header("Publicar Nova Notícia")
    titulo = st.text_input("Título da Notícia")
    conteudo = st.text_area("Conteúdo da Notícia", height=200)
    autor = st.text_input("Autor", placeholder="Ex: Prof. Silva")
    capa = st.file_uploader("Capa da Notícia (Imagem Frontal)", type=["png", "jpg", "jpeg", "webp"])
    
    if st.button("Publicar Notícia"):
        if titulo and conteudo:
            data_atual = datetime.now()
            doc_data = {
                "titulo": titulo,
                "conteudo": conteudo,
                "autor": autor,
                "data": data_atual,
                "tipo": "noticia"
            }
            
            # Se tiver imagem, sobe pro Firebase via File API/Storage
            if capa is not None:
                with st.spinner("☁️ Fazendo upload da Imagem de Capa..."):
                    img_bytes = capa.read()
                    import time
                    ext = capa.name.split('.')[-1]
                    nome_slug = titulo.lower().replace(' ', '_')[:20]
                    nome_arq = f"capa_{nome_slug}_{int(time.time())}.{ext}"
                    try:
                        url_capa = upload_to_storage(img_bytes, nome_arq, capa.type)
                        doc_data["imagem_url"] = url_capa
                    except Exception as e:
                        st.warning(f"Aviso: Erro inesperado ao hospedar imagem principal - {e}")
            
            adicionar_documento("conteudos", doc_data)
        else:
            st.warning("Preencha o título e o conteúdo!")
    render_persistent_footer()

def page_gerenciar_instrucoes():
    render_header_banner()
    st.header("Adicionar Instrução aos Docentes")
    instrucao_titulo = st.text_input("Assunto / Título")
    instrucao_detalhes = st.text_area("Detalhes da Instrução")
    prazo = st.date_input("Prazo de Execução (se houver)")
    
    if st.button("Enviar Instrução"):
        if instrucao_titulo and instrucao_detalhes:
            doc_data = {
                "titulo": instrucao_titulo,
                "detalhes": instrucao_detalhes,
                "prazo": str(prazo),
                "data_criacao": datetime.now(),
                "tipo": "instrucao"
            }
            adicionar_documento("conteudos", doc_data)
        else:
            st.warning("Preencha título e detalhes!")
    render_persistent_footer()



def page_todas_solicitacoes(tipo_pagina: str = "ambas"):
    if tipo_pagina == "criativos":
        pass
    elif tipo_pagina == "apoio":
        st.header("Central de Controle NEX - Solicitações de Apoio Técnico")
    else:
        st.header("Central de Controle NEX - Demandas de Criação de Conteúdos")

    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"] {
            max-width: 100% !important;
            padding-left: 1.25rem !important;
            padding-right: 1.25rem !important;
        }
        .status-card {
            border-radius: 10px;
            padding: 0.55rem 0.8rem;
            margin: 0.65rem 0 0.35rem 0;
            border: 1px solid #e5e7eb;
        }
        .status-card strong {
            font-size: 0.98rem;
            letter-spacing: 0.1px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    import pandas as pd

    STATUS_UI = {
        normalizar_status(STATUS_PENDENTE): {
            "label": "Pendente",
            "icon": "🟠",
            "color": "#b45309",
            "bg": "#fff7ed",
        },
        normalizar_status(STATUS_EM_PRODUCAO_ARTES): {
            "label": "Em andamento",
            "icon": "🔵",
            "color": "#1d4ed8",
            "bg": "#eff6ff",
        },
        normalizar_status(STATUS_CONCLUIDO): {
            "label": "Para conferir",
            "icon": "🟣",
            "color": "#7e22ce",
            "bg": "#faf5ff",
        },
        normalizar_status(STATUS_APROVADO_ENVIADO): {
            "label": "Finalizado",
            "icon": "🟢",
            "color": "#166534",
            "bg": "#f0fdf4",
        },
        normalizar_status(STATUS_ARQUIVADO): {
            "label": "Arquivado",
            "icon": "\u26ab",
            "color": "#4b5563",
            "bg": "#f9fafb",
        },
    }

    def normalizar_links_anexo(valor):
        if isinstance(valor, list):
            return [u.strip() for u in valor if isinstance(u, str) and u.strip().startswith("http")]
        if isinstance(valor, str) and valor.strip().startswith("http"):
            return [valor.strip()]
        return []

    def extrair_nome_arquivo(url):
        try:
            import urllib.parse
            path_part = str(url).split('/o/')[-1].split('?')[0]
            decoded_path = urllib.parse.unquote(path_part)
            return decoded_path.split('/')[-1]
        except Exception:
            return "Arquivo"

    def preparar_coluna_data(df_local, coluna):
        if coluna not in df_local.columns:
            return
        serie = pd.to_datetime(df_local[coluna], errors="coerce")
        try:
            if getattr(serie.dt, "tz", None) is not None:
                serie = serie.dt.tz_convert(None)
        except Exception:
            pass
        df_local[coluna] = serie

    def valor_texto(v):
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except Exception:
            pass
        return str(v).strip()

    def formatar_data_exibicao(v):
        if pd.isna(v):
            return "-"
        if isinstance(v, pd.Timestamp):
            return v.strftime("%d/%m/%Y - %H:%M")
        if isinstance(v, datetime):
            return v.strftime("%d/%m/%Y - %H:%M")
        txt = valor_texto(v)
        if not txt:
            return "-"
        dt = pd.to_datetime(txt, errors="coerce")
        if pd.isna(dt):
            return txt
        return dt.strftime("%d/%m/%Y - %H:%M")

    def resumo_anexos(urls):
        links = normalizar_links_anexo(urls)
        if not links:
            return "Sem anexos"
        return f"{len(links)} anexo(s)"

    def status_visual(status):
        meta = STATUS_UI.get(normalizar_status(status))
        if not meta:
            return f"⚪ {valor_texto(status) or 'Nao definido'}"
        return f"{meta['icon']} {meta['label']}"

    def badge_status_html(status):
        meta = STATUS_UI.get(normalizar_status(status))
        if not meta:
            return "<span style='padding:4px 8px;border-radius:999px;background:#f3f4f6;color:#374151;font-weight:600'>⚪ Nao definido</span>"
        return (
            "<span style='padding:4px 8px;border-radius:999px;"
            f"background:{meta['bg']};color:{meta['color']};font-weight:700'>"
            f"{meta['icon']} {meta['label']}</span>"
        )

    def section_header(titulo, status_ref, qtd):
        meta = STATUS_UI.get(normalizar_status(status_ref))
        if not meta:
            meta = {"icon": "⚪", "color": "#374151", "bg": "#f3f4f6"}
        st.markdown(
            (
                f"<div class='status-card' style='background:{meta['bg']}; border-left:6px solid {meta['color']}'>"
                f"<strong style='color:{meta['color']}'>{meta['icon']} {titulo}</strong>"
                f"<span style='margin-left:8px;color:#111827'>({qtd})</span>"
                "</div>"
            ),
            unsafe_allow_html=True,
        )

    def build_collection_data(collection_name, colunas_detalhe, date_cols):
        dados = listar_documentos(collection_name)
        if not dados:
            return None, None

        dados_formatados = []
        map_por_id = {}
        for sol in dados:
            doc_id = valor_texto(sol.get("id"))
            if not doc_id:
                continue
            if status_eh_arquivado(sol.get("status")):
                continue

            linha = sol.copy()
            linha["_doc_id"] = doc_id
            linha.pop("tentativas_ia", None)
            linha["anexos"] = normalizar_links_anexo(linha.get("anexos"))

            if "responsavel_nex" in colunas_detalhe and not valor_texto(linha.get("responsavel_nex")):
                linha["responsavel_nex"] = RESPONSAVEL_NEX_NAO_DEFINIDO

            if isinstance(linha.get("apoios_necessarios"), list):
                linha["apoios_necessarios"] = ", ".join(linha["apoios_necessarios"]) if linha["apoios_necessarios"] else "Nao informado"
            if "canais" in linha:
                linha["canais"] = channels_label(linha.get("canais"))

            linha["_status_norm"] = normalizar_status(linha.get("status") or STATUS_PENDENTE)
            linha["_status_visual"] = status_visual(linha.get("status"))
            linha["_anexos_markdown"] = montar_links_markdown(linha.get("anexos"))
            linha["_anexos_resumo"] = resumo_anexos(linha.get("anexos"))

            dados_formatados.append(linha)
            map_por_id[doc_id] = sol

        if not dados_formatados:
            return None, None

        df = pd.DataFrame(dados_formatados)
        for c in colunas_detalhe:
            if c not in df.columns:
                df[c] = None

        for c in date_cols:
            preparar_coluna_data(df, c)

        for coluna_ordem in ["data_solicitacao", "data_publicacao", "periodo_inicio", "periodo_fim"]:
            if coluna_ordem in df.columns:
                df = df.sort_values(by=coluna_ordem, ascending=False, na_position="last").reset_index(drop=True)
                break

        return df, map_por_id

    def selecionar_doc_na_tabela(df_subset, colunas_tabela, rename_map, date_cols, key_prefix):
        if df_subset.empty:
            return None

        df_table = df_subset[["_doc_id"] + colunas_tabela].copy()
        df_view = df_table.set_index("_doc_id")
        labels = {c: rename_map.get(c, c) for c in colunas_tabela}
        df_view = df_view.rename(columns=labels)

        coluna_config = {}
        for c in colunas_tabela:
            rotulo = labels[c]
            if c in date_cols:
                coluna_config[rotulo] = st.column_config.DatetimeColumn(
                    label=rotulo,
                    width="medium",
                    format="DD/MM/YYYY - HH:mm",
                )
            elif c == "descricao":
                coluna_config[rotulo] = st.column_config.TextColumn(label=rotulo, width="large")
            else:
                coluna_config[rotulo] = st.column_config.TextColumn(label=rotulo, width="medium")

        altura = min(560, max(180, 110 + (len(df_view) * 38)))
        widget_version = st.session_state.get(f"{key_prefix}_widget_version", 0)
        table_state = st.dataframe(
            df_view,
            key=f"{key_prefix}_table_v{widget_version}",
            width="stretch",
            height=altura,
            hide_index=True,
            row_height=40,
            column_config=coluna_config,
            on_select="rerun",
            selection_mode="single-row",
        )

        selecionadas = []
        if hasattr(table_state, "selection"):
            sel = table_state.selection
            if isinstance(sel, dict):
                selecionadas = sel.get("rows", []) or []
            elif hasattr(sel, "rows"):
                selecionadas = sel.rows or []

        if not selecionadas:
            return None

        idx = selecionadas[0]
        if idx < 0 or idx >= len(df_table):
            return None
        return valor_texto(df_table.iloc[idx]["_doc_id"])

    def bloco_acoes_detalhe(collection_name, sol_sel, key_prefix, table_prefix=""):
        if collection_name not in ["solicitacoes", "solicitacoes_eventos_transmissoes"]:
            return

        def _limpar_selecao():
            """Limpa session_state de sele\u00e7\u00e3o para evitar inconsist\u00eancia ap\u00f3s mudan\u00e7a de fila."""
            if not table_prefix:
                return
            for k in list(st.session_state.keys()):
                if k.startswith(table_prefix) and any(
                    tag in k for tag in ("active_table", "active_doc", "selected_doc")
                ):
                    st.session_state.pop(k, None)

        doc_id = valor_texto(sol_sel.get("id"))
        if not doc_id:
            st.warning("Solicita\u00e7\u00e3o sem ID. N\u00e3o foi poss\u00edvel habilitar a\u00e7\u00f5es.")
            return

        status_atual = sol_sel.get("status", STATUS_PENDENTE)
        status_norm = normalizar_status(status_atual)
        
        if status_eh_pendente(status_atual):
            # Acoes de atribuicao

            atual = valor_texto(sol_sel.get("responsavel_nex")) or RESPONSAVEL_NEX_NAO_DEFINIDO
            opcoes = [RESPONSAVEL_NEX_NAO_DEFINIDO] + [r for r in RESPONSAVEIS_NEX_OPCOES if r != RESPONSAVEL_NEX_NAO_DEFINIDO]
            if atual not in opcoes:
                opcoes.append(atual)

            c1, c2 = st.columns([2, 1])
            with c1:
                responsavel_novo = st.selectbox(
                        "Responsável NEX",
                    opcoes,
                    index=opcoes.index(atual),
                    key=f"{key_prefix}_form_nex_select",
                    label_visibility="collapsed"
                )
            with c2:
                confirmou = st.button(
                    "Confirmar",
                    type="primary",
                    use_container_width=True,
                    key=f"{key_prefix}_btn_confirmar_responsavel",
                    disabled=(responsavel_novo == RESPONSAVEL_NEX_NAO_DEFINIDO),
                )

            if confirmou:
                if responsavel_novo == RESPONSAVEL_NEX_NAO_DEFINIDO:
                    st.error("Selecione um respons\u00e1vel NEX v\u00e1lido.")
                    return

                campos = {
                    "responsavel_nex": responsavel_novo,
                    "status": STATUS_EM_PRODUCAO_ARTES,
                    "data_inicio_producao": datetime.now(),
                }

                ok = atualizar_documento(collection_name, doc_id, campos)
                if not ok:
                    st.error("Falha ao atualizar a solicita\u00e7\u00e3o.")
                    return

                sol_atualizada = {**sol_sel, **campos}
                erros = notificar_inicio_producao(sol_atualizada, responsavel_novo)
                mostrar_feedback_envio_email(
                    erros,
                    mensagem_ok="📧 E-mails de atualizacao enviados.",
                )

                st.success("Solicita\u00e7\u00e3o atualizada com sucesso.")
                _limpar_selecao()
                st.rerun()

            confirm_key = f"{key_prefix}_confirmar_arquivamento_doc"
            if st.session_state.get(confirm_key) != doc_id:
                arquivar = st.button(
                    "Arquivar solicita\u00e7\u00e3o",
                    type="secondary",
                    use_container_width=True,
                    key=f"{key_prefix}_btn_arquivar_solicitacao",
                    help="Remove esta solicita\u00e7\u00e3o das filas operacionais.",
                )
                if arquivar:
                    st.session_state[confirm_key] = doc_id
                    st.rerun()
            else:
                st.warning("Confirme o arquivamento desta solicita\u00e7\u00e3o. Ela sair\u00e1 das filas operacionais.")
                c_cancelar, c_confirmar = st.columns([1, 1])
                with c_cancelar:
                    cancelar_arquivo = st.button(
                        "Cancelar",
                        use_container_width=True,
                        key=f"{key_prefix}_btn_cancelar_arquivamento",
                    )
                with c_confirmar:
                    confirmar_arquivo = st.button(
                        "Confirmar arquivamento",
                        type="primary",
                        use_container_width=True,
                        key=f"{key_prefix}_btn_confirmar_arquivamento",
                    )

                if cancelar_arquivo:
                    st.session_state.pop(confirm_key, None)
                    st.rerun()

                if confirmar_arquivo:
                    campos = {
                        "status": STATUS_ARQUIVADO,
                        "data_arquivamento": datetime.now(),
                    }

                    ok = atualizar_documento(collection_name, doc_id, campos)
                    if not ok:
                        st.error("Falha ao arquivar a solicita\u00e7\u00e3o.")
                        return

                    st.session_state.pop(confirm_key, None)
                    st.success("Solicita\u00e7\u00e3o arquivada com sucesso.")
                    _limpar_selecao()
                    st.rerun()
            return

        if status_norm == normalizar_status(STATUS_EM_PRODUCAO_ARTES):
            st.markdown("### Submeter Resposta")
            links_ja_salvos = [
                u for u in (sol_sel.get("resposta_nex_anexos") or [])
                if isinstance(u, str) and u.strip()
            ]

            with st.form(key=f"{key_prefix}_form_resposta"):
                c1, c2, c3 = st.columns(3)
                
                with c1:
                    st.markdown("#### \U0001F4D1 Observa\u00e7\u00f5es")
                    valor_obs = valor_texto(sol_sel.get("resposta_nex_obs"))
                    texto_obs = st.text_area(
                        "Obs",
                        value=valor_obs,
                        height=280,
                        key=f"{key_prefix}_form_resposta_obs",
                        label_visibility="collapsed"
                    )

                with c2:
                    st.markdown("#### \U0001F4AC Texto da Resposta")
                    texto_inicial = valor_texto(sol_sel.get("resposta_nex_texto"))
                    texto_final = st.text_area(
                        "Texto",
                        value=texto_inicial,
                        height=280,
                        key=f"{key_prefix}_form_resposta_texto",
                        label_visibility="collapsed"
                    )

                with c3:
                    st.markdown("#### \U0001F4CE Anexar Entreg\u00e1veis")
                    arquivos = st.file_uploader(
                        "Upload",
                        type=["png", "jpg", "jpeg", "webp", "gif", "pdf"],
                        accept_multiple_files=True,
                        key=f"{key_prefix}_form_resposta_arquivos",
                        label_visibility="collapsed"
                    )
                    st.markdown("##### Antes de submeter para confer\u00eancia, fa\u00e7a este checklist final:")
                    st.markdown(
                        "1. **Escopo:** Confira se tudo solicitado est\u00e1 no material.\n"
                        "2. **Texto:** Revise portugu\u00eas, acentua\u00e7\u00e3o e erros de escrita.\n"
                        "3. **Visual:** Verifique identidade, elementos e a qualidade final."
                    )
                    if links_ja_salvos:
                        st.markdown("---")
                        render_links_markdown(links_ja_salvos, "J\u00e1 salvos")

                submeteu = st.form_submit_button("Submeter para confer\u00eancia", type="primary", use_container_width=True)

            if submeteu:
                texto_final = (texto_final or "").strip()
                texto_obs = (texto_obs or "").strip()
                links_resposta = list(links_ja_salvos)

                for arquivo in (arquivos or []):
                    mime = arquivo.type or "application/octet-stream"
                    url = upload_to_storage(arquivo.getvalue(), arquivo.name, mime)
                    if url:
                        links_resposta.append(url)

                if not texto_final and not links_resposta:
                    st.error("Informe um texto e/ou anexe entreg\u00e1veis para submeter.")
                    return

                campos = {
                    "resposta_nex_texto": texto_final,
                    "resposta_nex_obs": texto_obs,
                    "resposta_nex_anexos": links_resposta,
                    "status": STATUS_CONCLUIDO,
                    "data_resposta_nex": datetime.now(),
                }

                ok = atualizar_documento(collection_name, doc_id, campos)
                if not ok:
                    st.error("Falha ao enviar a resposta da solicita\u00e7\u00e3o.")
                    return

                sol_atualizada = {**sol_sel, **campos}
                erros = notificar_conclusao_para_coordenador(
                    sol_atualizada,
                    texto_final,
                    links_resposta,
                )
                mostrar_feedback_envio_email(
                    erros,
                    mensagem_ok="📧 E-mail enviado para a administracao.",
                )

                st.success("Resposta submetida para confer\u00eancia.")
                _limpar_selecao()
                st.rerun()
            return

        if status_norm == normalizar_status(STATUS_CONCLUIDO):
            st.markdown("### Conferir Resposta")
            with st.form(key=f"{key_prefix}_form_conferencia"):
                parecer = st.text_area(
                    "Observa\u00e7\u00f5es da confer\u00eancia (opcional)",
                    value=valor_texto(sol_sel.get("parecer_coordenador")),
                    height=140,
                    key=f"{key_prefix}_form_conferencia_parecer",
                )
                col_ok, col_back = st.columns(2)
                with col_ok:
                    aprovar = st.form_submit_button("OK e mover para Finalizado", type="primary")
                with col_back:
                    retornar = st.form_submit_button("Voltar para Em andamento")

            if aprovar:
                campos = {
                    "status": STATUS_APROVADO_ENVIADO,
                    "aprovado_final": True,
                    "parecer_coordenador": (parecer or "").strip(),
                    "data_aprovacao_final": datetime.now(),
                }
                ok = atualizar_documento(collection_name, doc_id, campos)
                if not ok:
                    st.error("Falha ao atualizar a solicita\u00e7\u00e3o.")
                    return

                sol_atualizada = {**sol_sel, **campos}
                resposta_texto = valor_texto(sol_sel.get("resposta_nex_texto"))
                resposta_anexos = [
                    u for u in (sol_sel.get("resposta_nex_anexos") or [])
                    if isinstance(u, str) and u.strip()
                ]
                erros = notificar_aprovacao_final_para_solicitante(
                    sol_atualizada,
                    resposta_texto,
                    resposta_anexos,
                    parecer,
                )
                mostrar_feedback_envio_email(
                    erros,
                    mensagem_ok="📧 E-mails de finalizacao enviados.",
                )

                st.success("Solicita\u00e7\u00e3o finalizada com sucesso.")
                _limpar_selecao()
                st.rerun()

            if retornar:
                campos = {
                    "status": STATUS_EM_PRODUCAO_ARTES,
                    "aprovado_final": False,
                    "parecer_coordenador": (parecer or "").strip(),
                    "data_retorno_producao": datetime.now(),
                }
                ok = atualizar_documento(collection_name, doc_id, campos)
                if not ok:
                    st.error("Falha ao atualizar a solicita\u00e7\u00e3o.")
                    return

                sol_atualizada = {**sol_sel, **campos}
                erros = notificar_retorno_para_producao(sol_atualizada, parecer)
                mostrar_feedback_envio_email(
                    erros,
                    mensagem_ok="📧 E-mail de retorno enviado para a equipe.",
                )

                st.warning("Solicita\u00e7\u00e3o retornou para Em andamento.")
                _limpar_selecao()
                st.rerun()


    def render_detalhes(sol_sel, row_sel, collection_name, colunas_detalhe, rename_map, date_cols, key_prefix, table_prefix=""):
        st.markdown("### Visualização completa da solicitação")
        
        with st.container(border=True):
            def valor_campo(campo):
                if campo in row_sel.index:
                    return row_sel.get(campo)
                return sol_sel.get(campo)

            def texto_campo(campo, padrao="-"):
                txt = valor_texto(valor_campo(campo))
                return txt or padrao

            def data_campo(campo):
                return formatar_data_exibicao(valor_campo(campo))

            @st.dialog("Descrição completa", width="large")
            def abrir_descricao_completa(texto: str):
                st.markdown(
                    """
                    <div style="text-align:center; margin-bottom: 0.75rem;">
                        <p style="margin:0; font-size:0.98rem; color:#64748b;">
                            Conteúdo completo da descrição da demanda
                        </p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown(texto)

            def render_texto_truncado(texto: str, chave: str, limite: int = 100):
                texto = valor_texto(texto)
                if not texto or texto == "-":
                    st.markdown("-")
                    return

                if len(texto) <= limite:
                    st.markdown(texto)
                    return

                st.markdown(f"{texto[:limite].rstrip()}...")
                if st.button("Ver mais", key=f"{chave}_btn_mais"):
                    abrir_descricao_completa(texto)

            def render_entrega_anexos(anexos_entrega):
                if not anexos_entrega:
                    st.caption("Sem anexos na entrega.")
                    return

                for link in anexos_entrega:
                    l_lower = str(link).lower()
                    nome_arq = extrair_nome_arquivo(str(link))
                    with st.container(border=True):
                        if any(ext in l_lower for ext in [".png", ".jpg", ".jpeg", ".webp"]) or ("alt=media" in l_lower and not any(a in l_lower for a in [".wav", ".mp3", ".pdf"])):
                            st.image(link, use_container_width=True)
                            st.markdown(f"[Abrir {nome_arq}]({link})")
                        elif any(ext in l_lower for ext in [".wav", ".mp3", ".ogg"]):
                            st.audio(link)
                            st.markdown(f"[Abrir {nome_arq}]({link})")
                        elif ".pdf" in l_lower:
                            st.markdown(f"📄 [{nome_arq}]({link})")
                        else:
                            st.markdown(f"🔗 [{nome_arq}]({link})")

            if collection_name == "solicitacoes":
                col_a, col_b, col_c = st.columns([1, 1, 1])

                with col_a:
                    st.markdown("#### \U0001F4CB Painel")
                    st.markdown(
                        f"**Status atual:** {badge_status_html(sol_sel.get('status', STATUS_PENDENTE))}",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"**Respons\u00e1vel NEX:** {valor_texto(sol_sel.get('responsavel_nex')) or RESPONSAVEL_NEX_NAO_DEFINIDO}"
                    )
                    st.markdown(f"**Tipo:** {texto_campo('tipo')}")
                    st.markdown(f"**Canais:** {texto_campo('canais')}")

                with col_b:
                    st.markdown("#### \U0001F464 Solicitante")
                    st.markdown(f"**Nome:** {texto_campo('solicitante')}")
                    st.markdown(f"**E-mail:** {texto_campo('email')}")
                    st.markdown(f"**Unidade:** {texto_campo('unidade')}")
                    st.markdown(f"**Perfil:** {texto_campo('solicitando_como')}")

                with col_c:
                    st.markdown("#### \U0001F4C5 Datas")
                    st.markdown(f"**Publicar em:** {data_campo('data_publicacao')}")
                    st.markdown(f"**Recebido em:** {data_campo('data_solicitacao')}")
                    st.markdown(f"**Produ\u00e7\u00e3o iniciada:** {data_campo('data_inicio_producao')}")
                    st.markdown(f"**Resposta enviada:** {data_campo('data_resposta_nex')}")

                # Area de Detalhamento e Acoes em Colunas (1:1:1)
                c_desc, c_anex, c_acoes = st.columns([1, 1, 1])
                
                with c_desc:
                    st.markdown("#### \U0001F4DD Descri\u00e7\u00e3o da demanda")
                    render_texto_truncado(texto_campo("descricao"), f"{key_prefix}_descricao")
                    
                    # Se for tecnico, mostra apoios aqui tambem
                    if collection_name == "solicitacoes_eventos_transmissoes":
                        apoios = texto_campo("apoios_necessarios", "")
                        if apoios:
                            st.markdown(f"**Apoios:** {apoios}")

                with c_anex:
                    st.markdown("#### \U0001F4CE Anexos")
                    anexos_md = row_sel.get("_anexos_markdown", "Sem anexos.")
                    st.markdown(anexos_md)

                with c_acoes:
                    st.markdown("#### \U0001F4AA\U0001F3FD Respons\u00e1vel NEX")
                    # Se pending, mostra o form aqui (pequeno)
                    if status_eh_pendente(sol_sel.get("status")):
                        bloco_acoes_detalhe(collection_name, sol_sel, key_prefix, table_prefix=table_prefix)
                    elif status_eh_concluido(sol_sel.get("status")):
                        nome_responsavel = valor_texto(sol_sel.get("responsavel_nex")) or RESPONSAVEL_NEX_NAO_DEFINIDO
                        st.info(f"Resposta submetida por **{nome_responsavel}**")
                    elif normalizar_status(sol_sel.get("status")) == normalizar_status(STATUS_APROVADO_ENVIADO):
                        nome_responsavel = valor_texto(sol_sel.get("responsavel_nex")) or RESPONSAVEL_NEX_NAO_DEFINIDO
                        st.success(f"Concluído por **{nome_responsavel}**")
                    else:
                        nome_responsavel = valor_texto(sol_sel.get("responsavel_nex")) or RESPONSAVEL_NEX_NAO_DEFINIDO
                        st.warning(f"Responsável NEX: **{nome_responsavel}**")

                status_finalizado = normalizar_status(sol_sel.get("status")) == normalizar_status(STATUS_APROVADO_ENVIADO)

                if status_eh_concluido(sol_sel.get("status")):
                    resposta_obs = valor_texto(sol_sel.get("resposta_nex_obs")) or "Sem observações."
                    resposta_texto = valor_texto(sol_sel.get("resposta_nex_texto")) or "Sem texto de entrega."
                    resposta_anexos = [
                        u for u in (sol_sel.get("resposta_nex_anexos") or [])
                        if isinstance(u, str) and u.strip()
                    ]

                    st.markdown("---")
                    c_resp, c_entrega, c_conf = st.columns([1, 1, 1])

                    with c_resp:
                        st.markdown("#### 📝 Resposta da produção")
                        st.markdown("**Observação**")
                        st.markdown(resposta_obs)
                        st.markdown("**Texto da entrega**")
                        st.markdown(resposta_texto)

                    with c_entrega:
                        st.markdown("#### 📎 Anexos da entrega")
                        render_entrega_anexos(resposta_anexos)

                    with c_conf:
                        st.markdown("#### ✅ Conferir resposta")
                        bloco_acoes_detalhe(collection_name, sol_sel, key_prefix, table_prefix=table_prefix)
                elif status_finalizado:
                    resposta_obs = valor_texto(sol_sel.get("resposta_nex_obs")) or "Sem observações."
                    resposta_texto = valor_texto(sol_sel.get("resposta_nex_texto")) or "Sem texto de entrega."
                    resposta_anexos = [
                        u for u in (sol_sel.get("resposta_nex_anexos") or [])
                        if isinstance(u, str) and u.strip()
                    ]
                    parecer_final = valor_texto(sol_sel.get("parecer_coordenador")) or "Sem observações da conferência."

                    st.markdown("---")
                    c_resp, c_entrega, c_conf = st.columns([1, 1, 1])

                    with c_resp:
                        st.markdown("#### 📝 Resposta da produção")
                        st.markdown("**Observação**")
                        st.markdown(resposta_obs)
                        st.markdown("**Texto da entrega**")
                        st.markdown(resposta_texto)

                    with c_entrega:
                        st.markdown("#### 📎 Anexos da entrega")
                        render_entrega_anexos(resposta_anexos)

                    with c_conf:
                        st.markdown("#### ✅ Conferência final")
                        st.markdown(f"**Parecer:** {parecer_final}")
                        st.markdown(f"**Finalizado em:** {data_campo('data_aprovacao_final')}")
                        st.success("Solicitação finalizada.")
                elif not status_eh_pendente(sol_sel.get("status")):
                    bloco_acoes_detalhe(collection_name, sol_sel, key_prefix, table_prefix=table_prefix)

                # Informacoes legadas ou genericas caso existam campos extras (apenas para Apoio Tecnico que usa loop)
                if collection_name != "solicitacoes":
                    with st.expander("Informa\u00e7\u00f5es Adicionais"):
                        for c in colunas_detalhe:
                            if c in {"status", "responsavel_nex", "descricao", "anexos", "apoios_necessarios"}:
                                continue
                            if c not in row_sel.index:
                                continue
                            valor = row_sel.get(c)
                            valor_fmt = formatar_data_exibicao(valor) if c in date_cols else (valor_texto(valor) or "-")
                            st.markdown(f"**{rename_map.get(c, c)}:** {valor_fmt}")

    def render_collection(
        collection_name,
        section_title,
        colunas_tabela,
        colunas_detalhe,
        rename_map,
        date_cols,
        table_prefix,
        separar_por_status=False,
    ):
        if section_title:
            st.subheader(section_title)
        df_all, map_por_id = build_collection_data(collection_name, colunas_detalhe, date_cols)
        if df_all is None or df_all.empty:
            st.info("Nenhuma solicita\u00e7\u00e3o encontrada nesta categoria.")
            return

        active_table_key = f"{table_prefix}_active_table"
        active_doc_key = f"{table_prefix}_active_doc"
        active_table = st.session_state.get(active_table_key, "")
        active_doc = st.session_state.get(active_doc_key, "")

        if not separar_por_status:
            table_id = f"{table_prefix}_geral"
            state_key = f"{table_prefix}_geral_selected_doc"
            doc_sel = selecionar_doc_na_tabela(
                df_all,
                colunas_tabela,
                rename_map,
                date_cols,
                table_id,
            )
            if doc_sel:
                st.session_state[state_key] = doc_sel
                st.session_state[active_table_key] = table_id
                st.session_state[active_doc_key] = doc_sel
                active_table = table_id
                active_doc = doc_sel
            else:
                st.session_state.pop(state_key, None)
                if st.session_state.get(active_table_key) == table_id:
                    st.session_state.pop(active_table_key, None)
                    st.session_state.pop(active_doc_key, None)
                    active_table = ""
                    active_doc = ""

            selected_doc = st.session_state.get(state_key)
            if active_table == table_id and selected_doc and selected_doc == active_doc and selected_doc in map_por_id:
                row_match = df_all[df_all["_doc_id"] == selected_doc]
                if not row_match.empty:
                    sol_sel = map_por_id.get(selected_doc, {})
                    row_sel = row_match.iloc[0]
                    render_detalhes(
                        sol_sel,
                        row_sel,
                        collection_name,
                        colunas_detalhe,
                        rename_map,
                        date_cols,
                        f"{table_prefix}_geral_detalhe",
                        table_prefix=table_prefix,
                    )
            return

        status_sections = [
            ("pendente", STATUS_PENDENTE, "Fila Pendente"),
            ("andamento", STATUS_EM_PRODUCAO_ARTES, "Fila Em Andamento"),
            ("conferir", STATUS_CONCLUIDO, "Fila Para Conferir"),
            ("finalizado", STATUS_APROVADO_ENVIADO, "Fila Finalizada"),
        ]

        sections = []
        conhecidos = set()
        for sec_key, status_ref, titulo in status_sections:
            table_id = f"{table_prefix}_{sec_key}"
            norm = normalizar_status(status_ref)
            conhecidos.add(norm)
            df_sec = df_all[df_all["_status_norm"] == norm].copy()
            sections.append(
                {
                    "sec_key": sec_key,
                    "status_ref": status_ref,
                    "titulo": titulo,
                    "table_id": table_id,
                    "df": df_sec,
                    "detail_key": f"{table_prefix}_{sec_key}_detalhe",
                }
            )

        df_outros = df_all[~df_all["_status_norm"].isin(conhecidos)].copy()
        if not df_outros.empty:
            sections.append(
                {
                    "sec_key": "outros",
                    "status_ref": "outros",
                    "titulo": "Fila Outros Status",
                    "table_id": f"{table_prefix}_outros",
                    "df": df_outros,
                    "detail_key": f"{table_prefix}_outros_detalhe",
                }
            )

        selecao_por_tabela = {}
        secao_por_tabela = {}
        slot_detalhe_por_tabela = {}

        for sec in sections:
            # Ordena\u00e7\u00e3o por fila: operacionais por prazo ASC, finalizadas por recebimento DESC
            df_sec = sec["df"]
            if not df_sec.empty:
                sn = normalizar_status(sec["status_ref"])
                if sn in (normalizar_status(STATUS_PENDENTE), normalizar_status(STATUS_EM_PRODUCAO_ARTES)):
                    if "data_publicacao" in df_sec.columns:
                        df_sec = df_sec.sort_values("data_publicacao", ascending=True, na_position="last").reset_index(drop=True)
                else:
                    if "data_solicitacao" in df_sec.columns:
                        df_sec = df_sec.sort_values("data_solicitacao", ascending=False, na_position="last").reset_index(drop=True)
                sec["df"] = df_sec

            table_id = sec["table_id"]
            state_key = f"{table_id}_selected_doc"
            secao_por_tabela[table_id] = sec

            section_header(sec["titulo"], sec["status_ref"], len(sec["df"]))
            doc_sel = selecionar_doc_na_tabela(
                sec["df"],
                colunas_tabela,
                rename_map,
                date_cols,
                table_id,
            )
            selecao_por_tabela[table_id] = doc_sel
            slot_detalhe_por_tabela[table_id] = st.empty()

        table_ids = [sec["table_id"] for sec in sections]
        prev_active_table = st.session_state.get(active_table_key, "")

        tabelas_selecionadas = [tid for tid in table_ids if selecao_por_tabela.get(tid)]
        novo_active_table = ""
        novo_active_doc = ""
        if tabelas_selecionadas:
            if prev_active_table in tabelas_selecionadas and len(tabelas_selecionadas) == 1:
                novo_active_table = prev_active_table
            elif prev_active_table in tabelas_selecionadas:
                alternativas = [tid for tid in tabelas_selecionadas if tid != prev_active_table]
                novo_active_table = alternativas[0] if alternativas else prev_active_table
            else:
                novo_active_table = tabelas_selecionadas[0]
            novo_active_doc = selecao_por_tabela.get(novo_active_table, "")

        reset_visual = False
        if novo_active_table:
            st.session_state[active_table_key] = novo_active_table
            st.session_state[active_doc_key] = novo_active_doc
            for tid in table_ids:
                state_key = f"{tid}_selected_doc"
                if tid == novo_active_table:
                    st.session_state[state_key] = novo_active_doc
                    continue
                st.session_state.pop(state_key, None)
                if selecao_por_tabela.get(tid):
                    ver_key = f"{tid}_widget_version"
                    st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
                    reset_visual = True
        else:
            st.session_state.pop(active_table_key, None)
            st.session_state.pop(active_doc_key, None)
            for tid in table_ids:
                state_key = f"{tid}_selected_doc"
                st.session_state.pop(state_key, None)
                if selecao_por_tabela.get(tid):
                    ver_key = f"{tid}_widget_version"
                    st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
                    reset_visual = True

        if reset_visual:
            st.rerun()

        active_table = st.session_state.get(active_table_key, "")
        active_doc = st.session_state.get(active_doc_key, "")
        if active_table and active_doc and active_table in secao_por_tabela and active_doc in map_por_id:
            sec = secao_por_tabela[active_table]
            row_match = sec["df"][sec["df"]["_doc_id"] == active_doc]
            if not row_match.empty:
                sol_sel = map_por_id.get(active_doc, {})
                row_sel = row_match.iloc[0]
                with slot_detalhe_por_tabela[active_table].container():
                    render_detalhes(
                        sol_sel,
                        row_sel,
                        collection_name,
                        colunas_detalhe,
                        rename_map,
                        date_cols,
                        sec["detail_key"],
                        table_prefix=table_prefix,
                    )


    if tipo_pagina in ("ambas", "criativos"):
        render_collection(
            collection_name="solicitacoes",
            section_title="",
            colunas_tabela=[
                "data_publicacao",
                "data_solicitacao",
                "solicitante",
                "tipo",
                "responsavel_nex",
                "_anexos_resumo",
            ],
            colunas_detalhe=[
                "status",
                "responsavel_nex",
                "data_publicacao",
                "data_solicitacao",
                "solicitante",
                "email",
                "tipo",
                "canais",
                "unidade",
                "solicitando_como",
                "descricao",
                "anexos",
            ],
            rename_map={
                "status": "Status",
                "responsavel_nex": "NEX",
                "data_publicacao": "Publicar em",
                "data_solicitacao": "Recebido em",
                "solicitante": "Solicitante",
                "email": "E-mail",
                "tipo": "Tipo",
                "canais": "Canais",
                "unidade": "Unidade",
                "solicitando_como": "Perfil",
                "descricao": "Descri\u00e7\u00e3o",
                "anexos": "Anexos",
                "_anexos_resumo": "Anexos",
            },
            date_cols=["data_solicitacao", "data_publicacao"],
            table_prefix="divulgacao",
            separar_por_status=True,
        )

    if tipo_pagina == "ambas":
        st.divider()

    if tipo_pagina in ("ambas", "apoio"):
        render_collection(
            collection_name="solicitacoes_eventos_transmissoes",
            section_title="Solicita\u00e7\u00f5es de Apoio T\u00e9cnico",
            colunas_tabela=[
                "_status_visual",
                "periodo_inicio",
                "periodo_fim",
                "solicitante",
                "tipo_evento",
                "local_evento",
                "responsavel_nex",
                "_anexos_resumo",
            ],
            colunas_detalhe=[
                "status",
                "responsavel_nex",
                "periodo_inicio",
                "periodo_fim",
                "data_solicitacao",
                "solicitante",
                "email",
                "tipo_evento",
                "local_evento",
                "apoios_necessarios",
                "unidade",
                "solicitando_como",
                "descricao",
                "anexos",
            ],
            rename_map={
                "_status_visual": "Etapa",
                "status": "Status",
                "responsavel_nex": "NEX",
                "periodo_inicio": "In\u00edcio",
                "periodo_fim": "Fim",
                "data_solicitacao": "Recebido em",
                "solicitante": "Solicitante",
                "email": "E-mail",
                "tipo_evento": "Evento",
                "local_evento": "Local",
                "apoios_necessarios": "Apoios",
                "unidade": "Unidade",
                "solicitando_como": "Perfil",
                "descricao": "Descri\u00e7\u00e3o",
                "anexos": "Anexos",
                "_anexos_resumo": "Anexos",
            },
            date_cols=["data_solicitacao", "periodo_inicio", "periodo_fim"],
            table_prefix="eventos",
            separar_por_status=False,
        )

def page_painel_controle_nex():
    st.header("📢 Comunica IME!")
    st.subheader("Painel de Controle de Demandas")
    tab_criativos, tab_apoio = st.tabs(["🎨 Solicitação de Criativos e Divulgação", "🎥 Solicitação de Apoio Técnico a Eventos e Transmissões"])
    with tab_criativos:
        page_todas_solicitacoes(tipo_pagina="criativos")
    with tab_apoio:
        page_todas_solicitacoes(tipo_pagina="apoio")


def page_sobre():
    render_header_banner()
    st.header("Sobre a Plataforma Comunica IME")
    st.markdown(
        """
### 1. Escopo geral
Esta plataforma organiza a rotina de comunicação do IME em um só lugar.
Ela serve para receber pedidos, distribuir responsabilidades, acompanhar a produção e registrar a entrega final.

Na prática, ela evita que uma demanda fique espalhada entre mensagens, e-mails e conversas paralelas.
Tudo passa a seguir um fluxo visível:

1. alguém envia um pedido;
2. a equipe assume a demanda;
3. o material é produzido;
4. a coordenação confere;
5. o pedido é finalizado.

O resultado é simples: fica mais fácil saber o que foi pedido, quem está fazendo, em que etapa está e o que já foi entregue.

### 2. Escopo técnico
O app está dividido em três camadas principais:

1. Camada de interface (Streamlit):
- formulários, tabelas, botões e visualizações.
- validação dos campos na entrada.
- interação operacional com status e responsáveis.

2. Camada de serviços (`app_core`):
- configuração (`config`).
- cliente HTTP (`http`).
- integração com Firestore e Storage (`firebase`).
- envio de notificações por e-mail (`emailer`).
- instruções de IA (`prompts`).

3. Camada de dados (Firebase):
- Firestore: metadados, textos, status, datas e responsáveis.
- Storage: arquivos anexados e entregáveis.

### 3. Fluxo técnico do back-end
Quando uma solicitação é enviada:

1. O formulário valida campos obrigatórios.
2. Os anexos são enviados para o Storage.
3. O documento é gravado no Firestore com status inicial.
4. O sistema pode enviar e-mail de confirmação.

Depois, no fluxo interno:

1. `Pendente`:
- responsável NEX é definido.
- status muda para `Em produção das artes`.

2. `Em produção das artes`:
- equipe registra texto de resposta e anexos de entrega.
- status muda para `Concluído` (fila para conferir).

3. `Concluído`:
- pode ir para finalização (`Aprovado e enviado`) ou retornar para andamento.

4. `Aprovado e enviado`:
- etapa final; sem ação operacional.

### 4. Coleções principais no banco
1. `solicitacoes`:
- pedidos de criativos e divulgação.
- campos típicos: `status`, `solicitante`, `email`, `tipo`, `descricao`, `canais`, `anexos`, `responsavel_nex`, datas e entrega.

2. `solicitacoes_eventos_transmissoes`:
- pedidos de apoio técnico e eventos.
- campos de período, local, apoios necessários e anexos.

3. `conteudos`:
- conteúdos para publicação no site.

### 5. Como os anexos funcionam
Os arquivos não ficam armazenados "dentro" do Firestore.
O arquivo sobe para o Storage e o Firestore guarda o link.
Isso melhora desempenho, reduz custo de leitura e facilita manutenção.

### 6. Modelos de IA usados no app
No módulo de proposta de conteúdo:

1. Texto multicanal:
- `gemini-3.1-flash-lite-preview`.
- gera versões para Instagram, WhatsApp, E-mail, LinkedIn e Site.

2. Imagem:
- `gemini-3-pro-image-preview`.
- gera arte com base no briefing e nos anexos.

As tentativas podem ser salvas em `tentativas_ia`.

### 7. Organização operacional atual
1. Entrada:
- Solicitação de Criativos e Divulgação.
- Solicitação de Apoio Técnico a Eventos e Transmissões.

2. Controle:
- Solicitações de Criativos (separadas por status).
- Solicitações de Apoio Técnico.
- Conferência e finalização inline na visualização das solicitações.

3. Apoio:
- Gerador de Proposta de Conteúdo.
- Publicar Notícia no site.

### 8. Resumo executivo
Pense no sistema como uma linha de produção digital:

1. Pedido entra.
2. Banco registra.
3. Equipe executa.
4. Coordenador confere.
5. Pedido finaliza.

Tudo fica registrado: o que foi pedido, quem fez, quando mudou de etapa e quais arquivos foram entregues.
"""
    )
    st.markdown(
        """
### 9. Exemplo prático de registros no banco
Abaixo estão exemplos mais fiéis aos documentos que o app pode manter no Firestore ao longo do ciclo completo.
A ideia aqui é mostrar os campos que podem existir depois que a demanda passa por criação, produção, conferência, finalização e apoio por IA.
"""
    )
    st.code(
        """
{
  "coleção": "solicitacoes",
  "documento": {
    "id": "abc123",
    "unidade": "Instituto de Matemática e Estatística",
    "solicitante": "Nome da pessoa",
    "email": "pessoa@ufba.br",
    "solicitando_como": "Docente",
    "tipo": "Evento Acadêmico",
    "status": "Aprovado e enviado",
    "responsavel_nex": "Nathalie",
    "urgencia": false,
    "aprovado_final": true,
    "data_solicitacao": "timestamp",
    "data_publicacao": "timestamp",
    "data_inicio_producao": "timestamp",
    "data_resposta_nex": "timestamp",
    "data_aprovacao_final": "timestamp",
    "data_retorno_producao": "timestamp",
    "descricao": "Texto da demanda",
    "canais": ["Instagram", "Site"],
    "anexos": ["https://.../anexo1.png", "https://.../anexo2.pdf"],
    "resposta_nex_obs": "Observações internas da produção.",
    "resposta_nex_texto": "Texto final entregue pela equipe.",
    "resposta_nex_anexos": ["https://.../entrega1.jpg", "https://.../entrega2.pdf"],
    "parecer_coordenador": "Aprovado sem ajustes.",
    "tentativas_ia": [
      {
        "data": "12/03/2026 10:15:00",
        "instrucoes_usadas": "Tom institucional, com chamada mais objetiva.",
        "opcoes": [
          {
            "id_opcao": 1,
            "legenda": "{\"instagram\":\"...\",\"site\":\"...\"}",
            "imagem_url": "https://.../ia_opcao1.png"
          }
        ]
      }
    ]
  }
}
        """.strip(),
        language="json",
    )
    st.code(
        """
{
  "coleção": "solicitacoes_eventos_transmissoes",
  "documento": {
    "id": "evt789",
    "unidade": "Instituto de Matemática e Estatística",
    "status": "Pendente",
    "solicitante": "Nome da pessoa",
    "email": "pessoa@ufba.br",
    "solicitando_como": "Servidor(a)",
    "tipo_evento": "Seminário",
    "local_evento": "Auditório Maria Zezé",
    "periodo_inicio": "timestamp",
    "periodo_fim": "timestamp",
    "data_solicitacao": "timestamp",
    "responsavel_nex": "Coordenação",
    "urgencia": true,
    "apoios_necessarios": ["Microfone", "Transmissão ao vivo"],
    "descricao": "Detalhes do apoio",
    "anexos": ["https://.../briefing.pdf"]
  }
}
        """.strip(),
        language="json",
    )
    st.markdown(
        """
### 10. Gatilhos de notificação (e-mails)
O sistema pode disparar e-mails em momentos-chave:

1. Confirmação de recebimento da solicitação.
2. Aviso de entrada em produção.
3. Aviso de submissão para conferência.
4. Aviso de aprovação final ao solicitante.
5. Aviso de retorno para produção quando há ajustes.

Isso mantém solicitante, coordenação e equipe alinhados sem operação manual.
"""
    )
    st.markdown(
        """
### 11. Como a seleção das tabelas funciona
Nas telas de controle:

1. Cada tabela mostra uma fila (status).
2. Ao selecionar uma linha, aparece a visualização completa daquela solicitação.
3. Só pode existir uma seleção ativa por vez na página.
4. Se você selecionar outra linha em outra fila, a anterior é desmarcada.
5. Se desmarcar a linha ativa, a visualização desaparece.
"""
    )
    st.markdown(
        """
### 12. Onde entram os modelos de IA no fluxo
Os modelos de IA não substituem o processo da demanda; eles aceleram a criação de proposta.

1. IA de texto: gera copys por canal.
2. IA de imagem: gera arte inicial para validação.
3. O resultado é salvo em histórico (`tentativas_ia`) para comparação.

Ou seja: a IA é um motor de apoio dentro do processo, não o processo completo.
"""
    )
    st.markdown(
        """
### 13. Diferença entre Front-end e Back-end neste app
Front-end (o que você vê):

1. Formulários.
2. Tabelas.
3. Botões de ação.
4. Visualização da solicitação.

Back-end (o que acontece por trás):

1. Validação da entrada.
2. Upload de anexos para Storage.
3. Persistência de dados no Firestore.
4. Mudança de status e rastreio por data/responsável.
5. Notificações por e-mail.
6. Integrações com IA quando necessário.
"""
    )
    st.markdown(
        """
### 14. Em uma frase
Esta plataforma organiza solicitações em um pipeline rastreável, com dados persistidos, anexos centralizados, controle por status e comunicação automatizada.
"""
    )
    st.markdown(
        """
### 13.1 Fluxo atual de e-mails
1. Nova solicitacao: e-mail de confirmacao para o solicitante e e-mail interno para administracao com copia oculta para estagiarios.
2. Mudanca de Pendente para Em andamento: e-mail de atualizacao para o solicitante e e-mail interno para administracao com copia oculta para estagiarios.
3. Submissao da resposta na fila Em andamento: e-mail apenas para a administracao avisando que a demanda entrou em conferencia.
4. OK final na fila Para conferir: e-mail ao solicitante com o conteudo gerado e e-mail de atualizacao para os estagiarios.
5. Retorno da fila Para conferir para Em andamento: e-mail para os estagiarios avisando que a solicitacao voltou para producao.
"""
    )
    render_persistent_footer()
def page_nova_solicitacao():
    render_header_banner()
    st.markdown("# Central de Solicitações")
    tab_publicacao, tab_apoio = st.tabs(["🎨 Solicitação de Criativos e Divulgação", "🎥 Solicitação de Apoio Técnico a Eventos e Transmissões"])
    with tab_publicacao:
        page_solicitar_publicacao()
    with tab_apoio:
        page_solicitar_apoio_eventos_transmissoes()

# Montagem das Paginas e Menu Lateral
pg = st.navigation({
    "Painel de Controle NEX": [
        st.Page(page_painel_controle_nex, title="Central de Demandas", icon="\U0001F4CB", default=True),
    ],
    "Em Construção": [
        st.Page(page_dashboard_solicitacoes, title="Gerador de Proposta de Conteúdo", icon="\U0001F4CA"),
        st.Page(page_adicionar_noticia, title="Publicar Notícia no site do DEST", icon="\U0001F4F0")
    ],
    "Sobre": [
        st.Page(page_sobre, title="Sobre a Plataforma", icon="\u2139")
    ]
})

pg.run()


from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from html import escape

from app_core.config import AppConfig
from app_core.domain import channels_label, normalize_channels


@dataclass
class SubmissionEmailPayload:
    solicitante: str
    email: str = ""
    unidade: str = ""
    solicitando_como: str = ""
    tipo: str = ""
    canais: list[str] = field(default_factory=list)
    descricao: str = ""
    data_publicacao: str = ""
    urgencia: bool = False
    audience: str = "interno"
    intro: str = ""
    closing: str = ""


class EmailNotifier:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _build_subject(self, payload: SubmissionEmailPayload) -> str:
        prefix = "[URGENTE] " if payload.urgencia else ""
        return f"{prefix}Comunica IME - {payload.tipo}"

    def _saudacao(self, payload: SubmissionEmailPayload) -> str:
        nome = (payload.solicitante or "").strip()
        if payload.audience == "solicitante" and nome:
            return f"Ola, {escape(nome.split()[0])}."
        return "Ola,"

    def _intro_padrao(self, payload: SubmissionEmailPayload) -> str:
        if payload.audience == "solicitante":
            return "Segue uma atualizacao sobre a sua solicitacao na plataforma."
        return "Uma atualizacao de demanda foi registrada na plataforma."

    def _fechamento_padrao(self, payload: SubmissionEmailPayload) -> str:
        if payload.audience == "solicitante":
            return "Voce recebera novas atualizacoes por e-mail sempre que houver mudanca relevante."
        return "Acompanhe a operacao pelo Painel de Controle de Demandas."

    def _build_html_body(self, payload: SubmissionEmailPayload) -> str:
        urgencia_badge = ""
        if payload.urgencia:
            urgencia_badge = (
                '<span style="background-color: #fff1e7; color: #c2410c; '
                'padding: 2px 8px; border-radius: 999px; font-weight: 700; '
                'font-size: 10px;">URGENTE</span>'
            )

        intro = escape((payload.intro or self._intro_padrao(payload)).strip())
        closing = escape((payload.closing or self._fechamento_padrao(payload)).strip())
        descricao = (payload.descricao or "").strip() or "Sem detalhes adicionais."
        descricao_html = escape(descricao).replace("\n", "<br>")
        canais = channels_label(normalize_channels(payload.canais))
        canais_row = ""
        if canais:
            canais_row = (
                "<tr>"
                '<td style="padding: 4px 0; color: #64748b;"><b>Canais:</b></td>'
                f'<td style="padding: 4px 0;">{escape(canais)}</td>'
                "</tr>"
            )

        faixa_topo = "#f58220" if payload.audience == "solicitante" else "#2f4a9e"
        titulo_bloco = "Resumo da solicitacao" if payload.audience == "solicitante" else "Resumo interno"

        return f"""
        <html>
            <body style="font-family: 'Source Sans Pro', 'Segoe UI', sans-serif; color: #334155; line-height: 1.5; margin: 0; padding: 0; background: #fffaf5;">
                <div style="max-width: 640px; margin: 24px auto; border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; background: #ffffff;">
                    <div style="background: {faixa_topo}; padding: 20px 24px;">
                        <h1 style="color: white; margin: 0; font-size: 24px;">Comunica IME!</h1>
                    </div>
                    <div style="padding: 24px;">
                        <p style="font-size: 16px; margin-top: 0;">{self._saudacao(payload)}</p>
                        <p style="font-size: 15px; margin-bottom: 18px;">{intro}</p>

                        <div style="background: #f8fafc; padding: 16px; border-radius: 10px; border-left: 4px solid {faixa_topo};">
                            <h3 style="margin: 0 0 12px; color: #1e3a8a; font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em;">{titulo_bloco}</h3>
                            <table style="width: 100%; font-size: 13px; border-collapse: collapse;">
                                <tr><td style="padding: 4px 0; color: #64748b; width: 140px;"><b>Solicitante:</b></td><td style="padding: 4px 0;">{escape(payload.solicitante or "Nao informado")}</td></tr>
                                <tr><td style="padding: 4px 0; color: #64748b;"><b>Unidade:</b></td><td style="padding: 4px 0;">{escape(payload.unidade or "Nao informado")}</td></tr>
                                <tr><td style="padding: 4px 0; color: #64748b;"><b>Solicitando como:</b></td><td style="padding: 4px 0;">{escape(payload.solicitando_como or "Nao informado")}</td></tr>
                                <tr><td style="padding: 4px 0; color: #64748b;"><b>Tipo:</b></td><td style="padding: 4px 0;">{escape(payload.tipo or "Nao informado")} {urgencia_badge}</td></tr>
                                {canais_row}
                                <tr><td style="padding: 4px 0; color: #64748b;"><b>Previsao:</b></td><td style="padding: 4px 0;">{escape(payload.data_publicacao or "Nao informado")}</td></tr>
                            </table>
                        </div>

                        <div style="margin-top: 18px;">
                            <h3 style="font-size: 14px; color: #253b80; margin-bottom: 8px;">Mensagem</h3>
                            <div style="font-size: 13px; color: #475569; background: #fff; border: 1px solid #e5e7eb; padding: 14px; border-radius: 10px;">
                                {descricao_html}
                            </div>
                        </div>

                        <p style="font-size: 13px; color: #64748b; margin-top: 20px;">{closing}</p>
                    </div>
                    <div style="background: #f8fafc; padding: 14px 20px; text-align: center; font-size: 11px; color: #94a3b8;">
                        Mensagem automatica enviada pelo sistema Comunica IME - UFBA.
                    </div>
                </div>
            </body>
        </html>
        """

    def _normalizar_destinatarios(self, emails: list[str] | None) -> list[str]:
        if not emails:
            return []

        normalizados = []
        vistos = set()
        for email in emails:
            email = str(email or "").strip()
            if not email or email in vistos:
                continue
            vistos.add(email)
            normalizados.append(email)
        return normalizados

    def send_email(
        self,
        payload: SubmissionEmailPayload,
        to_emails: list[str] | None = None,
        bcc_emails: list[str] | None = None,
    ) -> list[str]:
        if not self.config.email_enabled:
            return []

        if not self.config.smtp_host or not self.config.email_from:
            return [
                "EMAIL_ENABLED=true, mas SMTP_HOST/EMAIL_FROM nao foram configurados."
            ]

        to_list = self._normalizar_destinatarios(
            to_emails if to_emails is not None else ([payload.email] if payload.email else [])
        )
        bcc_list = []
        for email in self._normalizar_destinatarios(bcc_emails):
            if email not in to_list:
                bcc_list.append(email)

        all_recipients = to_list + bcc_list
        if not all_recipients:
            return ["Nao ha destinatarios para envio de e-mail."]

        message = EmailMessage()
        message["From"] = self.config.email_from
        message["To"] = ", ".join(to_list or [self.config.email_from])
        message["Subject"] = self._build_subject(payload)

        html_content = self._build_html_body(payload)
        message.set_content(
            "Para visualizar esta mensagem, utilize um cliente de e-mail com suporte a HTML."
        )
        message.add_alternative(html_content, subtype="html")

        try:
            with smtplib.SMTP(
                self.config.smtp_host, self.config.smtp_port, timeout=30
            ) as server:
                if self.config.smtp_use_tls:
                    server.starttls()
                if self.config.smtp_username:
                    server.login(self.config.smtp_username, self.config.smtp_password)
                server.send_message(message, to_addrs=all_recipients)
        except Exception as exc:
            return [f"Falha no envio de e-mail: {exc}"]

        return []

    def send_submission_notifications(
        self, payload: SubmissionEmailPayload
    ) -> list[str]:
        return self.send_email(
            payload,
            to_emails=[payload.email] if payload.email else [],
            bcc_emails=self.config.email_bcc,
        )

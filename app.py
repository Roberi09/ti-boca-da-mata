import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session
)
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime,
    Boolean, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from werkzeug.security import check_password_hash

load_dotenv()

app = Flask(__name__)

# ============================================================
# CONFIGURAÇÕES
# ============================================================

app.secret_key = os.environ.get("SECRET_KEY", "")

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip().lower()
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Número que receberá os chamados pelo WhatsApp (formato internacional)
WHATSAPP_TI = os.environ.get("WHATSAPP_TI", "").strip()

if not app.secret_key:
    raise RuntimeError("Defina SECRET_KEY como variável de ambiente.")

if not DATABASE_URL:
    raise RuntimeError("Defina DATABASE_URL como variável de ambiente.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=1800,
)

csrf = CSRFProtect(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per day"]
)

# ============================================================
# BANCO DE DADOS
# ============================================================

Base = declarative_base()

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = scoped_session(
    sessionmaker(bind=engine, autoflush=False, autocommit=False)
)


# ============================================================
# HORÁRIO DO BRASIL
# ============================================================

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora_brasil():
    """Retorna a data/hora atual no horário de Brasília."""
    # Remove o timezone para manter compatibilidade com as colunas
    # DateTime atuais do PostgreSQL.
    return datetime.now(FUSO_BRASIL).replace(tzinfo=None)


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True)
    usuario = Column(String(80), unique=True, nullable=False, index=True)
    nome = Column(String(150), nullable=False)
    senha_hash = Column(String(255), nullable=False)
    ativo = Column(Boolean, nullable=False, default=True)
    criado_em = Column(DateTime, nullable=False, default=agora_brasil)


class Chamado(Base):
    __tablename__ = "chamados"

    id = Column(Integer, primary_key=True)
    protocolo = Column(String(20), unique=True, nullable=False, index=True)
    nome = Column(String(150), nullable=False)
    setor = Column(String(100), nullable=False)
    telefone = Column(String(30), nullable=True)
    descricao = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="Aberto")
    mensagem_adm = Column(Text, nullable=False, default="")
    data_abertura = Column(DateTime, nullable=False, default=agora_brasil)
    data_atendimento = Column(DateTime, nullable=True)


class HistoricoChamado(Base):
    __tablename__ = "historico_chamados"

    id = Column(Integer, primary_key=True)
    chamado_id = Column(
        Integer,
        ForeignKey("chamados.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    acao = Column(String(50), nullable=False)
    status_anterior = Column(String(30), nullable=True)
    status_novo = Column(String(30), nullable=True)
    mensagem = Column(Text, nullable=True)
    criado_em = Column(DateTime, nullable=False, default=agora_brasil)


Base.metadata.create_all(bind=engine)


@app.teardown_appcontext
def remove_db_session(exception=None):
    SessionLocal.remove()


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def gerar_link_whatsapp(chamado):
    """Gera o link do WhatsApp com o chamado preenchido na mensagem."""
    if not WHATSAPP_TI:
        return ""

    mensagem_whatsapp = (
        "*NOVO CHAMADO DE TI*\n\n"
        f"Protocolo: {chamado.protocolo}\n\n"
        f"Solicitante: {chamado.nome}\n"
        f"Setor: {chamado.setor}\n"
        f"Telefone: {chamado.telefone or 'Não informado'}\n\n"
        "Problema:\n"
        f"{chamado.descricao}\n\n"
        f"Status: {chamado.status}"
    )

    return (
        f"https://wa.me/{WHATSAPP_TI}"
        f"?text={quote(mensagem_whatsapp, safe='')}"
    )

def gerar_protocolo():
    return f"CH-{uuid.uuid4().hex[:8].upper()}"


def validar_campos(nome, setor, descricao, telefone):
    if (
        len(nome) > 150
        or len(setor) > 100
        or len(descricao) > 3000
        or len(telefone) > 30
    ):
        return False

    if telefone and not re.fullmatch(r"[\d\s\(\)\-\+]*", telefone):
        return False

    return True


def chamado_para_dict(chamado):
    return {
        "id": chamado.id,
        "protocolo": chamado.protocolo,
        "nome": chamado.nome,
        "setor": chamado.setor,
        "telefone": chamado.telefone or "",
        "descricao": chamado.descricao,
        "status": chamado.status,
        "mensagem_adm": chamado.mensagem_adm or "",
        "data_abertura": (
            chamado.data_abertura.strftime("%d/%m/%Y %H:%M")
            if chamado.data_abertura else ""
        ),
        "data_atendimento": (
            chamado.data_atendimento.strftime("%d/%m/%Y %H:%M")
            if chamado.data_atendimento else ""
        ),
    }


def usuario_logado():
    usuario_id = session.get("usuario_id")

    if not usuario_id:
        return None

    db = SessionLocal()

    try:
        return (
            db.query(Usuario)
            .filter(
                Usuario.id == usuario_id,
                Usuario.ativo.is_(True)
            )
            .first()
        )
    finally:
        db.close()


def admin_autenticado():
    return bool(session.get("usuario_id"))


# ============================================================
# INICIALIZAÇÃO DO ADMINISTRADOR
# ============================================================

def garantir_admin_inicial():
    """
    Cria/atualiza o primeiro administrador usando as variáveis:
    ADMIN_USER, ADMIN_PASS_HASH e ADMIN_NAME.

    Depois de criar o usuário, as variáveis continuam podendo existir,
    mas a autenticação passa a ser feita pelo banco.
    """
    if not ADMIN_USER or not ADMIN_PASS_HASH:
        return

    nome = os.environ.get("ADMIN_NAME", "Administrador de TI").strip()

    db = SessionLocal()

    try:
        admin = (
            db.query(Usuario)
            .filter(Usuario.usuario == ADMIN_USER)
            .first()
        )

        if not admin:
            admin = Usuario(
                usuario=ADMIN_USER,
                nome=nome,
                senha_hash=ADMIN_PASS_HASH,
                ativo=True,
            )
            db.add(admin)
            db.commit()

        elif admin.senha_hash != ADMIN_PASS_HASH:
            admin.senha_hash = ADMIN_PASS_HASH
            admin.nome = nome
            admin.ativo = True
            db.commit()

    finally:
        db.close()


garantir_admin_inicial()


# ============================================================
# ROTAS PÚBLICAS
# ============================================================

@app.route("/")
def index():
    return render_template("publico.html")


@app.route("/abrir", methods=["POST"])
@limiter.limit("10 per minute")
def abrir_chamado():
    nome = request.form.get("nome", "").strip()
    setor = request.form.get("setor", "").strip()
    telefone = request.form.get("telefone", "").strip()
    descricao = request.form.get("descricao", "").strip()

    if not nome or not setor or not descricao:
        flash("Preencha todos os campos obrigatórios.", "erro")
        return redirect(url_for("index"))

    if not validar_campos(nome, setor, descricao, telefone):
        flash(
            "Um dos campos excede o tamanho permitido "
            "ou contém caracteres inválidos.",
            "erro"
        )
        return redirect(url_for("index"))

    db = SessionLocal()

    try:
        chamado = Chamado(
            protocolo=gerar_protocolo(),
            nome=nome,
            setor=setor,
            telefone=telefone,
            descricao=descricao,
            status="Aberto",
            mensagem_adm="",
            data_abertura=agora_brasil(),
        )

        db.add(chamado)
        db.flush()

        historico = HistoricoChamado(
            chamado_id=chamado.id,
            usuario_id=None,
            acao="Abertura",
            status_anterior=None,
            status_novo="Aberto",
            mensagem="Chamado aberto pelo solicitante.",
            criado_em=agora_brasil(),
        )

        db.add(historico)
        db.commit()
        db.refresh(chamado)

        whatsapp_link = gerar_link_whatsapp(chamado)

        return render_template(
            "protocolo.html",
            protocolo=chamado.protocolo,
            chamado=chamado_para_dict(chamado),
            whatsapp_link=whatsapp_link
        )

    except Exception:
        db.rollback()
        app.logger.exception("Erro ao abrir chamado.")
        flash(
            "Não foi possível registrar o chamado. Tente novamente.",
            "erro"
        )
        return redirect(url_for("index"))

    finally:
        db.close()


@app.route("/consultar", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def consultar():
    chamado = None

    if request.method == "POST":
        protocolo = request.form.get("protocolo", "").strip().upper()

        if not re.fullmatch(r"CH-[A-F0-9]{8}", protocolo):
            flash("Informe um protocolo válido.", "erro")
            return render_template("consulta.html", chamado=None)

        db = SessionLocal()

        try:
            chamado_obj = (
                db.query(Chamado)
                .filter(Chamado.protocolo == protocolo)
                .first()
            )

            if chamado_obj:
                chamado = chamado_para_dict(chamado_obj)
            else:
                flash("Protocolo não encontrado.", "erro")

        finally:
            db.close()

    return render_template("consulta.html", chamado=chamado)


# ============================================================
# LOGIN ADMINISTRATIVO
# ============================================================

@app.route("/admin", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip().lower()
        senha = request.form.get("senha", "")

        db = SessionLocal()

        try:
            admin = (
                db.query(Usuario)
                .filter(
                    Usuario.usuario == usuario,
                    Usuario.ativo.is_(True)
                )
                .first()
            )

            if admin and check_password_hash(admin.senha_hash, senha):
                session.clear()
                session["usuario_id"] = admin.id
                session["usuario_nome"] = admin.nome
                session.permanent = True

                return redirect(url_for("admin_painel"))

        finally:
            db.close()

        flash("Credenciais inválidas.", "erro")

    return render_template("admin_login.html")


@app.route("/admin/painel")
def admin_painel():
    if not admin_autenticado():
        flash("Acesso não autorizado.", "erro")
        return redirect(url_for("admin_login"))

    db = SessionLocal()

    try:
        chamados = (
            db.query(Chamado)
            .order_by(Chamado.id.desc())
            .all()
        )

        chamados_dict = [
            chamado_para_dict(c)
            for c in chamados
        ]

        return render_template(
            "admin_painel.html",
            chamados=chamados_dict,
            usuario_nome=session.get("usuario_nome", "")
        )

    finally:
        db.close()


@app.route("/admin/atualizar/<protocolo>", methods=["POST"])
def admin_atualizar(protocolo):
    if not admin_autenticado():
        flash("Acesso não autorizado.", "erro")
        return redirect(url_for("admin_login"))

    status = request.form.get("status", "").strip()
    mensagem = request.form.get("mensagem_adm", "").strip()

    status_validos = {
        "Aberto",
        "Em atendimento",
        "Resolvido",
        "Fechado",
    }

    if status not in status_validos:
        flash("Status inválido.", "erro")
        return redirect(url_for("admin_painel"))

    if len(mensagem) > 3000:
        flash("A mensagem excede o limite permitido.", "erro")
        return redirect(url_for("admin_painel"))

    db = SessionLocal()

    try:
        chamado = (
            db.query(Chamado)
            .filter(Chamado.protocolo == protocolo)
            .first()
        )

        if not chamado:
            flash("Chamado não encontrado.", "erro")
            return redirect(url_for("admin_painel"))

        usuario = (
            db.query(Usuario)
            .filter(Usuario.id == session["usuario_id"])
            .first()
        )

        status_anterior = chamado.status
        mensagem_anterior = chamado.mensagem_adm or ""

        chamado.status = status
        chamado.mensagem_adm = mensagem

        if status == "Em atendimento" and not chamado.data_atendimento:
            chamado.data_atendimento = agora_brasil()

        historico = HistoricoChamado(
            chamado_id=chamado.id,
            usuario_id=usuario.id if usuario else None,
            acao="Atualização",
            status_anterior=status_anterior,
            status_novo=status,
            mensagem=mensagem,
            criado_em=agora_brasil(),
        )

        db.add(historico)
        db.commit()

        flash(
            f"Chamado {protocolo} atualizado com sucesso!",
            "sucesso"
        )

    except Exception:
        db.rollback()
        app.logger.exception("Erro ao atualizar chamado.")
        flash("Não foi possível atualizar o chamado.", "erro")

    finally:
        db.close()

    return redirect(url_for("admin_painel"))


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

    app.run(
        debug=debug_mode,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
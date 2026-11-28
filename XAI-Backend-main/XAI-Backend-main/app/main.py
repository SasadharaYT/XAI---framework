from __future__ import annotations
import hashlib, hmac, json, os, secrets, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, create_engine, inspect as sqlalchemy_inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / '.env')
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{BASE_DIR / "industry_software.db"}')
JWT_SECRET = os.getenv('JWT_SECRET', 'local-development-secret')
ACCESS_TOKEN_MINUTES = int(os.getenv('ACCESS_TOKEN_MINUTES', '30'))
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = 'users'; id: Mapped[str] = mapped_column(String, primary_key=True); email: Mapped[str] = mapped_column(String, unique=True, index=True); name: Mapped[str] = mapped_column(String); password_hash: Mapped[str] = mapped_column(String); created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
class Project(Base):
    __tablename__ = 'projects'; id: Mapped[str] = mapped_column(String, primary_key=True); name: Mapped[str] = mapped_column(String); description: Mapped[str] = mapped_column(Text); type: Mapped[str] = mapped_column(String); agent_count: Mapped[int] = mapped_column(Integer, default=0); executions: Mapped[int] = mapped_column(Integer, default=0); pending_approvals: Mapped[int] = mapped_column(Integer, default=0); risk: Mapped[str] = mapped_column(String); last_activity: Mapped[str] = mapped_column(String); accent: Mapped[str] = mapped_column(String)
class Agent(Base):
    __tablename__ = 'agents'; id: Mapped[str] = mapped_column(String, primary_key=True); name: Mapped[str] = mapped_column(String); description: Mapped[str] = mapped_column(Text); framework: Mapped[str] = mapped_column(String); endpoint: Mapped[str] = mapped_column(String); status: Mapped[str] = mapped_column(String, default='Idle'); risk: Mapped[str] = mapped_column(String, default='Medium'); executions: Mapped[int] = mapped_column(Integer, default=0); last_run: Mapped[str] = mapped_column(String, default='Never'); project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True); metadata_json: Mapped[str] = mapped_column(Text, default='{}')
class Approval(Base):
    __tablename__ = 'approvals'; id: Mapped[str] = mapped_column(String, primary_key=True); agent: Mapped[str] = mapped_column(String); action: Mapped[str] = mapped_column(String); tool: Mapped[str] = mapped_column(String); parameters: Mapped[str] = mapped_column(Text); risk: Mapped[str] = mapped_column(String); status: Mapped[str] = mapped_column(String); request: Mapped[str] = mapped_column(Text); reason: Mapped[str] = mapped_column(Text); explanation: Mapped[str] = mapped_column(Text); reviewer: Mapped[str] = mapped_column(String); time: Mapped[str] = mapped_column(String); execution: Mapped[str] = mapped_column(String)
class TeamMember(Base):
    __tablename__ = 'team_members'; id: Mapped[str] = mapped_column(String, primary_key=True); name: Mapped[str] = mapped_column(String); email: Mapped[str] = mapped_column(String); initials: Mapped[str] = mapped_column(String); role: Mapped[str] = mapped_column(String); status: Mapped[str] = mapped_column(String); joined_date: Mapped[str] = mapped_column(String); project_ids: Mapped[str] = mapped_column(Text, default='')

class ProjectAPIKey(Base):
    __tablename__ = 'project_api_keys'; id: Mapped[str] = mapped_column(String, primary_key=True); project_id: Mapped[str] = mapped_column(String, index=True); key_hash: Mapped[str] = mapped_column(String, unique=True, index=True); key_prefix: Mapped[str] = mapped_column(String); name: Mapped[str] = mapped_column(String); created_by: Mapped[str] = mapped_column(String); created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc)); revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
class GovernanceEvaluation(Base):
    __tablename__ = 'governance_evaluations'; request_id: Mapped[str] = mapped_column(String, primary_key=True); project_id: Mapped[str] = mapped_column(String, index=True); body_hash: Mapped[str] = mapped_column(String); response_json: Mapped[str] = mapped_column(Text); created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
class GovernanceApproval(Base):
    __tablename__ = 'governance_approvals'; id: Mapped[str] = mapped_column(String, primary_key=True); request_id: Mapped[str] = mapped_column(String, unique=True, index=True); project_id: Mapped[str] = mapped_column(String, index=True); agent_id: Mapped[str] = mapped_column(String, index=True); agent_name: Mapped[str | None] = mapped_column(String, nullable=True); action_name: Mapped[str] = mapped_column(String); action_parameters_json: Mapped[str] = mapped_column(Text); reason: Mapped[str | None] = mapped_column(Text, nullable=True); metadata_json: Mapped[str] = mapped_column(Text, default='{}'); requested_at: Mapped[datetime] = mapped_column(DateTime); submitted_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc)); expires_at: Mapped[datetime] = mapped_column(DateTime); status: Mapped[str] = mapped_column(String, default='pending', index=True); reviewer_id: Mapped[str | None] = mapped_column(String, nullable=True); reviewer_comment: Mapped[str | None] = mapped_column(Text, nullable=True); reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True); body_hash: Mapped[str] = mapped_column(String)
class AuthorizationUse(Base):
    __tablename__ = 'authorization_uses'; jti: Mapped[str] = mapped_column(String, primary_key=True); request_id: Mapped[str] = mapped_column(String, index=True); project_id: Mapped[str] = mapped_column(String, index=True); consumed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class AutomaticAuthorization(Base):
    """Immutable server-issued authority for one persisted ALLOW evaluation."""
    __tablename__ = 'automatic_authorizations'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    project_id: Mapped[str] = mapped_column(String, index=True)
    agent_id: Mapped[str] = mapped_column(String)
    action_name: Mapped[str] = mapped_column(String)
    action_parameters_json: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    jti: Mapped[str] = mapped_column(String, unique=True)

class SignupIn(BaseModel): name: str = Field(min_length=2); email: str; password: str = Field(min_length=8)
class LoginIn(BaseModel): email: str; password: str; remember: bool = True
class ProjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default='', max_length=1000)
    type: str = Field(default='Agent system', min_length=2, max_length=80)
    risk: str = Field(default='Medium', pattern='^(Low|Medium|High|Critical)$')
    accent: str = Field(default='#198f70', pattern='^#[0-9a-fA-F]{6}$')
class AgentIn(BaseModel): name: str; description: str; framework: str | None = None; endpoint: str | None = None; project_id: str | None = None; metadata: dict[str, Any] = Field(default_factory=dict)
class ApprovalDecision(BaseModel): decision: str = Field(pattern='^(Approved|Rejected)$'); comment: str = ''
class TeamMemberIn(BaseModel): name: str; email: str; projectIds: list[str] = []
class MembershipIn(BaseModel): hasAccess: bool

class SDKActionIn(BaseModel):
    request_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    agent_name: str | None = None
    action_name: str = Field(min_length=1)
    action_parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    requested_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

class ProjectAPIKeyIn(BaseModel): name: str = Field(default='SDK key', min_length=1, max_length=100)
class AuthorizationVerifyIn(BaseModel): request_id: str; approval_id: str | None = None; authorization_receipt: str = Field(min_length=1); project_id: str; agent_id: str; action_name: str; action_parameters: dict[str, Any] = Field(default_factory=dict)

def model_dict(row: Any) -> dict[str, Any]: return {k: v for k, v in row.__dict__.items() if not k.startswith('_')}
def hash_password(value: str) -> str:
    salt = secrets.token_bytes(16); return f'{salt.hex()}${hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 180000).hex()}'
def verify_password(value: str, stored: str) -> bool:
    try:
        salt, digest = stored.split('$', 1); actual = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), 180000).hex(); return hmac.compare_digest(actual, digest)
    except ValueError: return False
def user_out(user: User) -> dict[str, Any]: return {'id': user.id, 'email': user.email, 'name': user.name, 'is_email_verified': True, 'has_password': True, 'created_at': user.created_at.isoformat()}
def issue_token(user: User) -> str: return jwt.encode({'sub': user.id, 'exp': datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES)}, JWT_SECRET, algorithm='HS256')
def auth_response(user: User, response: Response) -> dict[str, Any]:
    response.set_cookie('refresh_token', secrets.token_urlsafe(48), httponly=True, samesite='lax', max_age=2592000, path='/api/v1/auth')
    return {'status': 'authenticated', 'access_token': issue_token(user), 'token_type': 'bearer', 'expires_in': ACCESS_TOKEN_MINUTES * 60, 'user': user_out(user)}
bearer = HTTPBearer(auto_error=False)
def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    if not credentials: raise HTTPException(401, 'Authentication required')
    try: user_id = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=['HS256']).get('sub')
    except jwt.PyJWTError: raise HTTPException(401, 'Invalid or expired token')
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user: raise HTTPException(401, 'User not found')
        return user

app = FastAPI(title='Industry Software API', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv('FRONTEND_URL', 'http://localhost:5173')], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Avoid echoing submitted action values in SDK endpoint validation errors."""
    if request.url.path == '/api/v1/agents' or request.url.path.startswith((
        '/api/v1/oversight/', '/api/v1/authorizations/', '/api/v1/approvals/'
    )):
        return JSONResponse(status_code=422, content={'detail': 'Invalid request'})
    return JSONResponse(status_code=422, content={'detail': exc.errors()})

@app.on_event('startup')
def startup():
    Base.metadata.create_all(engine)
    _migrate_agent_project_columns()
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.email == 'demo@example.com')):
            db.add(User(id='usr-demo', email='demo@example.com', name='Demo User', password_hash=hash_password('DemoPass123')))
            db.commit()
        if not db.scalar(select(TeamMember).where(TeamMember.email == 'demo@example.com')):
            db.add(TeamMember(id='usr-demo', name='Demo User', email='demo@example.com', initials='DU', role='Team Leader', status='Active', joined_date='Today', project_ids='atlas-research,ledger-ops,prism-support'))
            db.commit()

def _migrate_agent_project_columns() -> None:
    """Add project ownership to existing SQLite agent rows without recreating the database."""
    inspector = sqlalchemy_inspect(engine)
    if 'agents' not in inspector.get_table_names(): return
    columns = {column['name'] for column in inspector.get_columns('agents')}
    with engine.begin() as connection:
        if 'project_id' not in columns:
            connection.execute(text('ALTER TABLE agents ADD COLUMN project_id VARCHAR'))
        if 'metadata_json' not in columns:
            connection.execute(text("ALTER TABLE agents ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"))
        for agent_id, project_id in [('agt-01','atlas-research'),('agt-02','prism-support'),('agt-03','ledger-ops'),('agt-04','atlas-research')]:
            connection.execute(text('UPDATE agents SET project_id = :project_id WHERE id = :agent_id AND project_id IS NULL'), {'project_id': project_id, 'agent_id': agent_id})
@app.get('/health')
def health(): return {'status': 'ok'}
@app.post('/api/v1/auth/signup')
def signup(payload: SignupIn, response: Response):
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == payload.email.lower())): raise HTTPException(409, 'Email is already registered')
        user = User(id=f'usr-{secrets.token_hex(8)}', email=payload.email.lower(), name=payload.name.strip(), password_hash=hash_password(payload.password)); db.add(user); db.commit(); db.refresh(user); return auth_response(user, response)
@app.post('/api/v1/auth/login')
def login(payload: LoginIn, response: Response):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == payload.email.lower()))
        if not user or not verify_password(payload.password, user.password_hash): raise HTTPException(401, 'Invalid email or password')
        return auth_response(user, response)
@app.post('/api/v1/auth/refresh')
def refresh(request: Request, response: Response):
    if not request.cookies.get('refresh_token'): raise HTTPException(401, 'No active session')
    with SessionLocal() as db:
        user = db.scalar(select(User).order_by(User.created_at))
        if not user: raise HTTPException(401, 'No active session')
        return auth_response(user, response)
@app.post('/api/v1/auth/logout', status_code=204)
def logout(response: Response): response.delete_cookie('refresh_token', path='/api/v1/auth')
@app.get('/api/v1/auth/me')
def me(user: User = Depends(current_user)): return user_out(user)
@app.get('/api/v1/auth/oauth/providers')
def oauth_providers(): return {'providers': []}

@app.get('/api/v1/projects')
def projects(_: User = Depends(current_user)):
    with SessionLocal() as db: return [model_dict(x) for x in db.scalars(select(Project)).all()]
@app.post('/api/v1/projects', status_code=201)
def create_project(payload: ProjectIn, user: User = Depends(current_user)):
    """Create a project and grant the creator active team-leader access."""
    with SessionLocal() as db:
        base = ''.join(ch.lower() if ch.isalnum() else '-' for ch in payload.name).strip('-')
        base = '-'.join(part for part in base.split('-') if part) or 'project'
        project_id = base
        suffix = 2
        while db.get(Project, project_id) is not None:
            project_id = f'{base}-{suffix}'
            suffix += 1
        row = Project(id=project_id, name=payload.name.strip(), description=payload.description.strip(), type=payload.type.strip(), agent_count=0, executions=0, pending_approvals=0, risk=payload.risk, last_activity='Just now', accent=payload.accent)
        db.add(row)
        member = db.scalar(select(TeamMember).where(TeamMember.email == user.email))
        if member is None:
            member = TeamMember(id=user.id, name=user.name, email=user.email, initials=''.join(part[0] for part in user.name.split())[:2].upper(), role='Team Leader', status='Active', joined_date='Today', project_ids=project_id)
            db.add(member)
        else:
            ids = [item for item in member.project_ids.split(',') if item]
            if project_id not in ids:
                ids.append(project_id)
            member.project_ids = ','.join(ids)
        db.commit(); db.refresh(row)
        return model_dict(row)
@app.get('/api/v1/projects/{project_id}/overview')
def overview(project_id: str, _: User = Depends(current_user)):
    with SessionLocal() as db:
        if not db.get(Project, project_id): raise HTTPException(404, 'Project not found')
        return {'agents': [model_dict(x) for x in db.scalars(select(Agent).where(Agent.project_id == project_id)).all()], 'approvals': [model_dict(x) for x in db.scalars(select(Approval)).all()], 'executions': [], 'activity': []}
@app.get('/api/v1/agents')
def get_agents(_: User = Depends(current_user)):
    with SessionLocal() as db: return [model_dict(x) for x in db.scalars(select(Agent)).all()]
@app.post('/api/v1/agents', status_code=201)
def add_agent(payload: AgentIn, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None: raise HTTPException(401, 'Authentication required')
    with SessionLocal() as db:
        from .governance import _reject_credentials, _require_membership, project_key_for_token
        key = project_key_for_token(db, credentials.credentials)
        if key is not None:
            _reject_credentials(payload.metadata)
            project_id = payload.project_id or key.project_id
            if project_id != key.project_id: raise HTTPException(403, 'Project does not match the API key')
            if db.get(Project, project_id) is None: raise HTTPException(404, 'Project not found')
            row = Agent(id=f'agt-{secrets.token_hex(6)}', name=payload.name.strip(), description=payload.description, framework=payload.framework or 'Python SDK', endpoint=payload.endpoint or '', project_id=project_id, metadata_json=json.dumps(payload.metadata, sort_keys=True))
            db.add(row)
            project = db.get(Project, project_id)
            project.agent_count = (project.agent_count or 0) + 1
            db.commit(); db.refresh(row)
            return {'agent_id': row.id, 'project_id': project_id, 'name': row.name, 'created_at': datetime.now(timezone.utc).isoformat()}
        user = current_user(credentials)
        if payload.project_id:
            _require_membership(db, user, payload.project_id)
        row = Agent(id=f'agt-{secrets.token_hex(6)}', name=payload.name, description=payload.description, framework=payload.framework or '', endpoint=payload.endpoint or '', project_id=payload.project_id, metadata_json=json.dumps(payload.metadata, sort_keys=True)); db.add(row); db.commit(); db.refresh(row); return model_dict(row)
@app.delete('/api/v1/agents/{agent_id}', status_code=204)
def delete_agent(agent_id: str, user: User = Depends(current_user)):
    with SessionLocal() as db:
        row = db.get(Agent, agent_id)
        if row is None: raise HTTPException(404, 'Agent not found')
        if row.project_id:
            from .governance import _require_membership
            _require_membership(db, user, row.project_id, manage_keys=True)
        project = db.get(Project, row.project_id) if row.project_id else None
        db.delete(row)
        if project is not None: project.agent_count = max(0, (project.agent_count or 0) - 1)
        db.commit()
@app.get('/api/v1/approvals')
def get_approvals(_: User = Depends(current_user)):
    with SessionLocal() as db: return [model_dict(x) for x in db.scalars(select(Approval)).all()]
@app.post('/api/v1/approvals/{approval_id}/decision')
def decide(approval_id: str, payload: ApprovalDecision, user: User = Depends(current_user)):
    with SessionLocal() as db:
        row = db.get(Approval, approval_id)
        if not row: raise HTTPException(404, 'Approval not found')
        if row.status != 'Pending': raise HTTPException(409, 'Only pending requests can be reviewed')
        from .governance import record_dashboard_decision
        record_dashboard_decision(db, approval_id, payload.decision, payload.comment, user)
        row.status = payload.decision; row.reviewer = user.name; db.commit(); db.refresh(row); return model_dict(row)
@app.get('/api/v1/team-members')
def get_team(_: User = Depends(current_user)):
    with SessionLocal() as db: return [{**model_dict(x), 'projectIds': [v for v in x.project_ids.split(',') if v]} for x in db.scalars(select(TeamMember)).all()]
@app.post('/api/v1/team-members', status_code=201)
def add_team(payload: TeamMemberIn, _: User = Depends(current_user)):
    with SessionLocal() as db:
        row = TeamMember(id=f'usr-{secrets.token_hex(6)}', name=payload.name, email=str(payload.email), initials=''.join(x[0] for x in payload.name.split())[:2].upper(), role='Team Member', status='Invited', joined_date='Pending invitation', project_ids=','.join(payload.projectIds)); db.add(row); db.commit(); db.refresh(row); return {**model_dict(row), 'projectIds': payload.projectIds}
@app.patch('/api/v1/team-members/{member_id}/projects/{project_id}')
def membership(member_id: str, project_id: str, payload: MembershipIn, _: User = Depends(current_user)):
    with SessionLocal() as db:
        row = db.get(TeamMember, member_id)
        if not row: raise HTTPException(404, 'Team member not found')
        ids = {v for v in row.project_ids.split(',') if v}; (ids.add(project_id) if payload.hasAccess else ids.discard(project_id)); row.project_ids = ','.join(ids); db.commit(); db.refresh(row); return {**model_dict(row), 'projectIds': list(ids)}


from .governance import router as governance_router

app.include_router(governance_router)

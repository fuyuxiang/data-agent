"""
认证 API
"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_token,
)
from app.models.models import User, UserWorkspace, Workspace
from app.schemas.schemas import (
    UserCreate,
    UserResponse,
    UserLogin,
    Token,
    WorkspaceCreate,
    WorkspaceResponse,
)

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """获取当前用户"""
    from app.core.logging import get_logger
    logger = get_logger(__name__)

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )

    logger.info(f"Token: {token[:50]}...")

    token_data = decode_token(token)
    logger.info(f"Token data: {token_data}")

    if token_data is None or token_data.user_id is None:
        logger.error("Token 解码失败或 user_id 为空")
        raise credentials_exception

    result = await db.execute(
        select(User).where(User.id == token_data.user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception
    return user


@router.post("/register", response_model=UserResponse)
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    """用户注册"""
    # 检查用户名是否存在
    result = await db.execute(
        select(User).where(User.username == user_data.username)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )

    # 创建用户
    hashed_password = get_password_hash(user_data.password)
    user = User(
        username=user_data.username,
        email=user_data.email,
        full_name=user_data.full_name,
        hashed_password=hashed_password,
    )
    db.add(user)
    await db.flush()

    # 创建默认工作空间
    workspace = Workspace(name="默认工作空间", description="系统创建的个人工作空间")
    db.add(workspace)
    await db.flush()

    # 关联用户与工作空间
    user_workspace = UserWorkspace(
        user_id=user.id,
        workspace_id=workspace.id,
        role="owner",
    )
    db.add(user_workspace)

    await db.commit()
    await db.refresh(user)

    return user


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """用户登录"""
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用",
        )

    # 获取用户的工作空间
    uw_result = await db.execute(
        select(UserWorkspace).where(UserWorkspace.user_id == user.id)
    )
    workspaces = uw_result.scalars().all()

    # 如果用户没有工作空间，创建一个默认工作空间
    if not workspaces:
        workspace = Workspace(name="默认工作空间", description="系统创建的个人工作空间")
        db.add(workspace)
        await db.flush()

        user_workspace = UserWorkspace(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        db.add(user_workspace)
        await db.commit()

        workspace_id = workspace.id
    else:
        workspace_id = workspaces[0].workspace_id

    # 创建 token
    access_token = create_access_token(
        data={"user_id": user.id, "workspace_id": workspace_id},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return current_user


@router.get("/workspaces", response_model=list[WorkspaceResponse])
async def get_workspaces(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取用户的工作空间列表"""
    result = await db.execute(
        select(UserWorkspace)
        .where(UserWorkspace.user_id == current_user.id)
    )
    user_workspaces = result.scalars().all()

    workspace_ids = [uw.workspace_id for uw in user_workspaces]
    if not workspace_ids:
        return []

    ws_result = await db.execute(
        select(Workspace).where(Workspace.id.in_(workspace_ids))
    )
    workspaces = ws_result.scalars().all()

    return workspaces


@router.post("/workspaces", response_model=WorkspaceResponse)
async def create_workspace(
    workspace_data: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建工作空间"""
    workspace = Workspace(**workspace_data.model_dump())
    db.add(workspace)
    await db.flush()

    # 关联用户
    user_workspace = UserWorkspace(
        user_id=current_user.id,
        workspace_id=workspace.id,
        role="owner",
    )
    db.add(user_workspace)

    await db.commit()
    await db.refresh(workspace)

    return workspace

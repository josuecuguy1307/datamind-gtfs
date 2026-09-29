from __future__ import annotations

import streamlit as st
from typing import Optional, Dict, Any


class AuthService:
    """
    Thin Streamlit wrapper around st.session_state.

    Authentication is handled BEFORE reaching views.
    This service only exposes the current user and helpers.
    """

    # -----------------------------
    # Session access
    # -----------------------------
    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """
        Returns the authenticated user stored in session_state,
        or None if not logged in.
        """
        return st.session_state.get("auth.user")

    def is_authenticated(self) -> bool:
        return st.session_state.get("auth.user") is not None

    def get_roles(self) -> list[str]:
        return st.session_state.get("auth.roles", [])

    def has_role(self, role: str) -> bool:
        return role in self.get_roles()

    def require_admin(self) -> None:
        if not self.has_role("admin"):
            raise PermissionError("Admin role required")

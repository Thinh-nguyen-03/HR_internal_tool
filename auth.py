import os
import time
import secrets
from datetime import timedelta
from typing import Optional, Dict
from functools import wraps

from flask import session, redirect, request
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user


class User(UserMixin):
    def __init__(self, username: str):
        self.id = username
        self.username = username


class AuthManager:
    def __init__(self, server, redis_client=None):
        self.server = server
        self.login_manager = LoginManager()
        self.login_manager.init_app(server)
        self.login_manager.login_view = '/login'
        self.login_manager.session_protection = 'strong'
        
        # Rate limiting configuration
        self.redis = redis_client
        self.login_attempts: Dict[str, list] = {}
        self.max_attempts = 5
        self.lockout_duration = 300
        
        # Configure session with security-first defaults
        secret_key = os.getenv('SECRET_KEY')
        if not secret_key:
            raise ValueError(
                "SECRET_KEY environment variable must be set. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        
        server.config.update(
            SECRET_KEY=secret_key,
            SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'True').lower() == 'true',
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE='Lax',
            PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
            SESSION_COOKIE_NAME='hr_tool_session'
        )
        
        @self.login_manager.user_loader
        def load_user(username):
            if username == os.getenv('APP_USERNAME'):
                return User(username)
            return None
    
    def verify_password(self, username: str, password: str) -> bool:
        expected_username = os.getenv('APP_USERNAME')
        expected_password = os.getenv('APP_PASSWORD')
        
        if not expected_username or not expected_password:
            print("ERROR: APP_USERNAME or APP_PASSWORD not set in environment")
            return False
        
        username_match = secrets.compare_digest(username, expected_username)
        password_match = secrets.compare_digest(password, expected_password)
        
        return username_match and password_match
    
    def is_rate_limited(self, username: str) -> bool:
        """Check if user is rate limited (works across multiple workers with Redis)."""
        # Use Redis for distributed rate limiting if available
        if self.redis:
            try:
                key = f"login_attempts:{username}"
                attempts = self.redis.llen(key)
                if attempts >= self.max_attempts:
                    return True
                return False
            except Exception as e:
                print(f"Redis rate limit check failed, falling back to in-memory: {e}")
                # Fall through to in-memory check
        
        # Fallback to in-memory (for development/single worker)
        current_time = time.time()
        
        if username not in self.login_attempts:
            return False
        
        # Remove attempts outside lockout window
        self.login_attempts[username] = [
            attempt_time for attempt_time in self.login_attempts[username]
            if current_time - attempt_time < self.lockout_duration
        ]
        
        if len(self.login_attempts[username]) >= self.max_attempts:
            return True
        
        return False
    
    def record_failed_attempt(self, username: str) -> None:
        """Record a failed login attempt (persists across workers with Redis)."""
        # Use Redis for distributed tracking if available
        if self.redis:
            try:
                key = f"login_attempts:{username}"
                self.redis.rpush(key, time.time())
                self.redis.expire(key, self.lockout_duration)
                return
            except Exception as e:
                print(f"Redis failed attempt recording failed, falling back to in-memory: {e}")
                # Fall through to in-memory tracking
        
        # Fallback to in-memory (for development/single worker)
        if username not in self.login_attempts:
            self.login_attempts[username] = []
        self.login_attempts[username].append(time.time())
    
    def clear_failed_attempts(self, username: str) -> None:
        """Clear failed login attempts on successful login."""
        # Clear from Redis if available
        if self.redis:
            try:
                key = f"login_attempts:{username}"
                self.redis.delete(key)
            except Exception as e:
                print(f"Redis clear attempts failed: {e}")
        
        # Also clear from in-memory cache
        if username in self.login_attempts:
            self.login_attempts[username] = []
    
    def attempt_login(self, username: str, password: str) -> tuple[bool, str]:
        if not username or not password:
            return False, "Please enter both username and password"
        
        if self.is_rate_limited(username):
            return False, "Too many failed attempts. Please try again in 5 minutes"
        
        if self.verify_password(username, password):
            user = User(username)
            login_user(user, remember=True, duration=timedelta(hours=8))
            self.clear_failed_attempts(username)
            session.permanent = True
            return True, "Login successful"
        else:
            self.record_failed_attempt(username)
            remaining_attempts = self.max_attempts - len(self.login_attempts.get(username, []))
            if remaining_attempts > 0:
                return False, f"Invalid credentials. {remaining_attempts} attempts remaining"
            else:
                return False, "Too many failed attempts. Please try again in 5 minutes"
    
    def logout(self) -> None:
        logout_user()
        session.clear()


def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


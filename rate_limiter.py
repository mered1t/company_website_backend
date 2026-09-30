from slowapi import Limiter
from slowapi.util import get_remote_address


def get_client_ip(request) -> str:
    # За Cloudflare (Render) настоящий IP клиента. Этот заголовок нельзя подделать снаружи.
    return request.headers.get("cf-connecting-ip") or get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
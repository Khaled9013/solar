from django.http import JsonResponse


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A bootstrap/reset password can only reach account management until replaced.
        if (request.user.is_authenticated and request.user.must_change_password
                and request.path.startswith("/api/")
                and request.path not in ("/api/me/", "/api/password/", "/api/logout/")):
            response = JsonResponse({"error": "Change your temporary password to continue.",
                                     "code": "password_change_required"}, status=403)
        else:
            response = self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; connect-src 'self' blob: data:; font-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response["Cross-Origin-Opener-Policy"] = "same-origin"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response["Cache-Control"] = "no-store"
        return response

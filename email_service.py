import resend

from config import settings

resend.api_key = settings.resend_api_key


import resend

from config import settings

resend.api_key = settings.resend_api_key


def send_invitation_email(to_email: str, organization_name: str, token: str) -> None:
    accept_url = f"https://koracrm.com/invitations/accept?token={token}"

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": to_email,
        "subject": f"You've been invited to join {organization_name}",
        "html": f"""
            <p>You've been invited to join <b>{organization_name}</b> on our CRM platform.</p>
            <p><a href="{accept_url}">Click here to accept the invitation</a></p>
            <p>This link is valid for 7 days.</p>
        """,
    })
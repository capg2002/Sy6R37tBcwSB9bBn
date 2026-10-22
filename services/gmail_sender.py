"""Gmail SMTP with TLS. Credentials stay in environment variables."""
# Import email construction, environment credentials, TLS, and SMTP support.
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
import os
import re
import smtplib
import ssl


# Identify failures classified as safe for a later delivery attempt.
class RetryableDeliveryError(Exception):
    """We know this attempt did not send the message."""


# Identify failures where delivery may have succeeded and automatic retry must stop.
class UncertainDeliveryError(Exception):
    """Gmail may have accepted the message; do not automatically send again."""


# Validate a single plain email address without display names or recipient lists.
def address(value, label):

    # Reject addresses containing whitespace, list separators, or an invalid basic address shape.
    if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
        raise ValueError(f'{label} must contain one plain email address.')
    
    return value


# Send plain-text report emails through Gmail using TLS and environment credentials.
class GmailSender:
    # Load and validate the sender, recipient, and app password from environment variables.
    def __init__(self):

        self.sender = address(os.environ.get('GMAIL_SENDER', '').strip(), 'GMAIL_SENDER')
        self.recipient = address(os.environ.get('REPORT_RECIPIENT', '').strip(), 'REPORT_RECIPIENT')
        self.password = ''.join(os.environ.get('GMAIL_APP_PASSWORD', '').split())
        # Reject startup when the Gmail app password is empty.
        if not self.password:
            raise ValueError('GMAIL_APP_PASSWORD is missing.')

    # Construct a report email and classify failures by whether delivery could have occurred.
    def send(self, subject, body, message_id, recipient = None):

        # Validate the recipient and populate message identity, timestamp, and plain-text content.
        recipient = address(recipient or self.recipient, 'recipient')
        message = EmailMessage()
        message['From'], message['To'] = self.sender, recipient
        message['Subject'], message['Message-ID'] = subject, message_id
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message.set_content(body)

        client = None

        # Keep connection cleanup separate from delivery outcome classification.
        try:
            # Connect over TLS and authenticate before submitting the message.
            try:
                client = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20,
                                         context=ssl.create_default_context())
                client.login(self.sender, self.password)

            # Classify connection or login failure as retryable because submission has not started.
            except (OSError, smtplib.SMTPException) as error:
                raise RetryableDeliveryError(type(error).__name__) from None
            
            # Submit the message to the single recipient and inspect rejection information.
            try:
                refused = client.send_message(message, from_addr=self.sender, to_addrs=[recipient])
                # Treat an explicitly refused recipient as a retryable delivery failure.
                if refused:
                    raise RetryableDeliveryError('RecipientRefused')
                
            # Convert an SMTP response error into a retryable failure containing only its status code.
            except smtplib.SMTPResponseException as error:
                raise RetryableDeliveryError(f'SMTP {error.smtp_code}') from None
            
            # Treat an explicit recipient refusal as a retryable failure.
            except smtplib.SMTPRecipientsRefused:
                raise RetryableDeliveryError('RecipientRefused') from None
            
            # Treat transport or other SMTP failures during submission as an uncertain delivery outcome.
            except (OSError, smtplib.SMTPException) as error:
                raise UncertainDeliveryError(type(error).__name__) from None
            
        # Always attempt connection cleanup without changing an already determined delivery result.
        finally:
            # A failure after DATA was accepted must not turn success into retry.
            # Close only when an SMTP client was successfully created.
            if client is not None:
                # Attempt to release the SMTP connection.
                try: client.close()
                # Ignore an operating-system error raised during connection cleanup.
                except OSError: pass

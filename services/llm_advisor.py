"""Read-only Gemini advisor with strict local validation and no tools."""
# Import JSON handling and environment access for the Gemini adapter.
import json
import os

# Define allowed recommendations and the versioned advisory-only instruction text.
MODES = ('hybrid', 'dca_only', 'pause_entries')
PROMPT_VERSION = 'btc-mode-advisor-gemini-v2'
PROMPT = '''You are an advisory-only reviewer of a BTC paper-trading system.
Use only the supplied numerical snapshot. Never claim access to news, future
prices, or tools. Never promise returns. Recommend exactly one permitted mode:
hybrid allows DCA and swing entries; dca_only allows DCA entries; pause_entries
blocks all new buys. Existing swing exits and protective stops continue in ALL
modes. DCA-only still adds BTC exposure; it is not a no-risk mode. Budget pools
and the reserve remain unchanged. GARCH sigma forecasts volatility, not direction.
Bearish crossover ENTRY is an intentional configured rule, not permission to
short BTC. Input candle times and portfolio quote times may differ as labelled.
Missing indicators, missing returns and uncertain execution reduce evidence;
use pause_entries with a data-quality explanation if core inputs are unusable.
Avoid unnecessary mode changes without supporting evidence. Return one short
reason (at most 600 characters) referencing the supplied inputs, and a list of
limitations (at most 5 items, each at most 160 characters). No orders, prices to
trade at, sizing instructions, invented facts, or extra fields. Your response is
recorded for evaluation only and does not control trading.'''

# Define the expected JSON response fields and disallow additional properties.
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'recommended_mode': {'type': 'string', 'enum': list(MODES)},
        'reason': {'type': 'string'},
        'limitations': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['recommended_mode', 'reason', 'limitations'],
}


# Validate the response structure, permitted recommendation, and explanation lengths locally.
def validate_advice(value):

    # Require exactly the expected advisory fields in a dictionary.
    if not isinstance(value, dict) or set(value) != set(SCHEMA['required']):
        raise ValueError('Unexpected advisory fields.')
    
    # Reject recommendations outside the supported strategy modes.
    if value['recommended_mode'] not in MODES:
        raise ValueError('Invalid recommended mode.')
    
    reason = value['reason']
    # Require a nonblank explanation of at most 600 characters.
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600:
        raise ValueError('Invalid explanation.')
    
    limits = value['limitations']
    # Allow at most five nonblank limitation strings of at most 160 characters each.
    if not isinstance(limits, list) or len(limits) > 5 or any(
            not isinstance(v, str) or not 1 <= len(v.strip()) <= 160 for v in limits):
        raise ValueError('Invalid limitations.')
    
    return value


# Build the approved public input while withholding balances and strategy settings.
def public_context(context):
    import math
    """Allowlist numeric market information; exclude account and strategy details."""
    market = context.get('market', {})

    # List the market fields permitted in the external advisory request.
    keys = ('Close', 'ATR', 'SMA_20', 'SMA_50', 'PREV_SMA_20', 'PREV_SMA_50',
            'return_1h_fraction', 'return_24h_fraction', 'missing_or_invalid_hours_in_window',
            'observed_ohlc')
    
    # Return finite numeric values and replace unsupported or non-finite values with None.
    def number(v):
        return v if isinstance(v, (int, float)) and math.isfinite(v) else None
    
    garch = context.get('garch', {})

    # Copy only the permitted mode, market values, and volatility estimate into the request context.
    return {'current_mode': context.get('current_mode') if context.get('current_mode') in MODES else None,
            'market': {k: number(market.get(k)) for k in keys},
            'sigma_next_hour': number(garch.get('sigma_next_hour_fraction')),
            'note': 'Account balances and strategy settings deliberately withheld.'}


# Represent exhausted Gemini quotas or rate limits without automatic retries.
class GeminiRateLimitError(Exception): pass
# Represent Gemini authentication or access failures.
class GeminiAuthenticationError(Exception): pass
# Represent unsuccessful Gemini HTTP responses outside the specialized categories.
class GeminiHTTPError(Exception): pass
# Represent connection or transport failures without exposing request details.
class GeminiTransportError(Exception): pass


# Provide a read-only Gemini client with filtered input and strict response validation.
class GeminiAdvisor:
    # Load the API key, validate the model identifier, and create a reusable HTTP session.
    def __init__(self, settings):

        import math
        import re
        import requests
        self.key = os.environ.get('GEMINI_API_KEY', '').strip()
        # Reject initialization when the Gemini API key is missing.
        if not self.key:
            raise ValueError('GEMINI_API_KEY is missing from .env.')
        
        # Require a Gemini model identifier containing only the permitted characters.
        if not re.fullmatch(r'gemini-[a-z0-9.-]+', settings['model']):
            raise ValueError('Expected a Gemini model identifier.')
        
        self.settings = settings
        self.client = requests.Session()  # No automatic retries.

    # Submit one filtered advisory request and return a validated status, advice, and metadata.
    def advise(self, context):
        import math
        import requests
        # Build the system prompt, filtered context, response schema, and output-token limit.
        body = {
            'systemInstruction': {'parts': [{'text': PROMPT}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(public_context(context), allow_nan=False)}]}],
            'generationConfig': {'responseMimeType': 'application/json',
                                 'responseJsonSchema': SCHEMA,
                                 'maxOutputTokens': self.settings['max_output_tokens']},
        }
        # Submit the request with connection/read timeouts and redirects disabled.
        try:
            response = self.client.post(
                'https://generativelanguage.googleapis.com/v1beta/models/' + self.settings['model'] + ':generateContent',
                headers={'x-goog-api-key': self.key}, json=body,
                timeout=(5, self.settings['timeout_seconds']), allow_redirects=False)
            
        # Replace transport exceptions with a generic error that omits request credentials.
        except requests.RequestException:
            raise GeminiTransportError('Gemini request failed.') from None
        
        # Report quota exhaustion without automatically repeating the request.
        if response.status_code == 429:
            raise GeminiRateLimitError('Gemini quota exhausted; no automatic retry.')
        
        # Report authentication or access rejection separately from other HTTP failures.
        if response.status_code in (401, 403):
            raise GeminiAuthenticationError('Gemini authentication/access failed.')
        
        # Reject all remaining HTTP responses that are not successful.
        if response.status_code != 200:
            raise GeminiHTTPError(
                f"Gemini returned HTTP {response.status_code}."
            )
        
        # Decode the response and gather provider metadata before validating its generated content.
        try:
            data = response.json()
            meta = {'provider': 'gemini', 'response_id': data.get('responseId'),
                    'model_returned': data.get('modelVersion'), 'usage': data.get('usageMetadata'),
                    'submitted_input': public_context(context)}
            
            # Return a refusal when the provider blocks the input prompt.
            if data.get('promptFeedback', {}).get('blockReason'):
                return 'refused', None, meta
            
            candidates = data.get('candidates', [])

            # Require exactly one generated candidate to avoid ambiguous response selection.
            if len(candidates) != 1:
                return 'invalid_response', None, meta
            
            candidate = candidates[0]
            finish = candidate.get('finishReason')

            # Classify explicit provider safety or content blocks as refusals.
            if finish in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII'):
                return 'refused', None, meta
            
            # Treat any other non-STOP finish reason as an incomplete response.
            if finish != 'STOP':
                return 'incomplete', None, meta
            
            parts = candidate.get('content', {}).get('parts', [])

            # Reject tool-call content because this adapter accepts advisory text only.
            if any('functionCall' in p for p in parts):
                return 'invalid_response', None, meta
            
            # Join non-thought text parts, parse their JSON, and validate the resulting recommendation.
            output = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
            advice = validate_advice(json.loads(output))

        # Return a minimal invalid-response result for malformed or unexpected provider data.
        except (ValueError, TypeError, AttributeError, KeyError):
            return 'invalid_response', None, {'provider': 'gemini'}
        
        # Return the validated advice with its associated provider metadata.
        return 'ok', advice, meta

    # Close the reusable HTTP session.
    def close(self):
        self.client.close()

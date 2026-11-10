"""Provider-specific documented billing metadata; never infer actual debits."""
OPENAI_SOURCE = 'https://developers.openai.com/api/docs/models/gpt-6-luna'
# The user's plan screen; the public docs page listed lower values on the same day.
RELAY_SOURCE = 'user plan screen, relaymodels.com'
CHECKED = '2026-10-31'

def relay_coefficient(model):
    return {'gpt-6-luna': 0.38, 'gpt-6.1-sol': 3, 'gpt-6-astra': 10,
            'gpt-6-sol': 3, 'gpt-5.6-sol': 4, 'gpt-5.6-terra': 1.5,
            'gpt-5.6-luna': 0.5}.get(model)

def pricing(profile=None):
    name = profile.name if profile else 'OpenAI'
    model = profile.model if profile else 'gpt-6-luna'
    incoming = outgoing = None
    source, status, coefficient = None, 'UNKNOWN', None
    if name == 'OpenAI' and model == 'gpt-6-luna':
        incoming, outgoing = 0.10, 0.50
        source, status = OPENAI_SOURCE, 'CONFIRMED_USD_2026-10-25_STANDARD'
    elif name == 'RelayModels':
        source, coefficient = RELAY_SOURCE, relay_coefficient(model)
    elif name == 'Custom OpenAI-compatible':
        incoming, outgoing = profile.input_price, profile.output_price
        if incoming is not None and outgoing is not None:
            source, status = 'USER_SUPPLIED', 'USER_SUPPLIED'
    return dict(input=incoming, output=outgoing, currency='USD/MTok' if
                incoming is not None and outgoing is not None else None,
                source=source, status=status, coefficient=coefficient, checked=CHECKED)

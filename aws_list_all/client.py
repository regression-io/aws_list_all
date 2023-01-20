import boto3

_CLIENTS = {}


def get_client(service, region='us-east-1', profile='default'):
    """Return (cached) boto3 clients for this service and this region"""
    key = (service, region, profile)
    session = None
    if key not in _CLIENTS:
        session = boto3.Session(region_name=region, profile_name=profile)
        _CLIENTS[key] = session.client(service)
    return _CLIENTS[key], session

"""Use only currently valid anchors from the existing OS trust store."""

import ssl, time


def verified_context():
    default = ssl.create_default_context()
    info = default.get_ca_certs()
    binary = default.get_ca_certs(binary_form=True)
    if len(info) != len(binary):
        raise RuntimeError("CA_STORE_METADATA_MISMATCH")
    now = time.time()
    valid = [
        der
        for item, der in zip(info, binary)
        if ssl.cert_time_to_seconds(item["notBefore"])
        <= now
        <= ssl.cert_time_to_seconds(item["notAfter"])
    ]
    if not valid:
        raise RuntimeError("NO_VALID_OS_TRUST_ANCHORS")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_flags = ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_TRUSTED_FIRST
    context.load_verify_locations(cadata="\n".join(ssl.DER_cert_to_PEM_cert(x) for x in valid))
    return context

# coding: utf-8
from __future__ import unicode_literals, division, absolute_import, print_function

import sys
import hashlib

from asn1crypto.keys import PublicKeyInfo
from asn1crypto import core

from .._ffi import new, null, buffer_from_bytes, deref, bytes_from_buffer, struct, struct_bytes, cast, unwrap, buffer_from_unicode
from ._cng import bcrypt, bcrypt_const, handle_error, open_alg_handle, close_alg_handle
from .._int import int_to_bytes, int_from_bytes, fill_width
from ..keys import parse_public, parse_certificate, parse_private, parse_pkcs12
from ..errors import SignatureError, PrivateKeyError

if sys.version_info < (3,):
    str_cls = unicode  #pylint: disable=E0602
    byte_cls = str
else:
    str_cls = str
    byte_cls = bytes



class PrivateKey():

    bcrypt_key_handle = None
    algo = None
    curve = None
    bit_size = None

    def __init__(self, bcrypt_key_handle, algo, curve=None, bit_size=None):
        self.bcrypt_key_handle = bcrypt_key_handle
        self.algo = algo
        self.curve = curve
        self.bit_size = bit_size

    def __del__(self):
        if self.bcrypt_key_handle:
            res = bcrypt.BCryptDestroyKey(self.bcrypt_key_handle)
            handle_error(res)
            self.bcrypt_key_handle = None


class PublicKey(PrivateKey):

    pass


class Certificate(PublicKey):

    pass


class Signature(core.Sequence):
    _fields = [
        ('r', core.Integer),
        ('s', core.Integer),
    ]

    @classmethod
    def from_bcrypt(cls, data):
        """
        Reads a signature from a byte string created by Microsoft's
        BCryptSignHash() function.

        :param data:
            A byte string from BCryptSignHash()

        :return:
            A Signature object
        """

        r = int_from_bytes(data[0:len(data)//2])
        s = int_from_bytes(data[len(data)//2:])
        return cls({'r': r, 's': s})

    def to_bcrypt(self):
        """
        Dumps a signature to a byte string compatible with Microsoft's
        BCryptVerifySignature() function.

        :return:
            A byte string compatible with BCryptVerifySignature()
        """

        r_bytes = int_to_bytes(self['r'].native)
        s_bytes = int_to_bytes(self['s'].native)

        int_byte_length = max(len(r_bytes), len(s_bytes))
        r_bytes = fill_width(r_bytes, int_byte_length)
        s_bytes = fill_width(s_bytes, int_byte_length)

        return r_bytes + s_bytes



def load_certificate(source, source_type):
    """
    Loads an x509 certificate into a format usable with rsa_verify()

    :param source:
        A byte string of file contents or a unicode string filename

    :param source_type:
        A unicode string describing the source - "file" or "bytes"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A Certificate object
    """

    if source_type not in ('file', 'bytes'):
        raise ValueError('source_type is not one of "file" or "bytes"')

    if source_type == 'file':
        with open(source, 'rb') as f:
            source = f.read()

    elif not isinstance(source, byte_cls):
        raise ValueError('source is not a byte string')

    certificate, algo = parse_certificate(source)
    return _load_key(certificate['tbs_certificate']['subject_public_key_info'], algo, Certificate)


def _load_key(key_object, algo, container):
    """
    Loads a public or private key into a format usable with various functions

    :param key_object:
        An asn1crypto.keys.PublicKeyInfo or asn1crypto.keys.PrivateKeyInfo
        object

    :param algo:
        A unicode string of "rsa", "dsa" or "ec"

    :param container:
        The class of the object to hold the bcrypt_key_handle

    :return:
        A PrivateKey, PublicKey or Certificate object, based on container
    """

    key_type = 'public' if isinstance(key_object, PublicKeyInfo) else 'private'

    alg_handle = None
    key_handle = None
    curve_name = None
    bit_size = None

    try:
        if algo == 'ec':
            curve_type, curve_name = key_object.curve
            if curve_type != 'named':
                raise PrivateKeyError('Windows only supports EC keys using named curves')
            if curve_name not in ('secp256r1', 'secp384r1', 'secp521r1'):
                raise PrivateKeyError('Windows only supports EC keys using the named curves secp256r1, secp384r1 and secp521r1')

        elif algo == 'dsa':
            ver_info = sys.getwindowsversion()
            pair = (ver_info.major, ver_info.minor)
            if key_object.bit_size > 1024 and pair < (6, 2):
                raise PrivateKeyError('Windows Vista, 7 and Server 2008 only support DSA keys based on SHA1 (1024 bits or less) - this key is based on %s and is %s bits' % (key_object.hash_algo.upper(), key_object.bit_size))
            elif key_object.bit_size == 2048 and key_object.hash_algo == 'sha1':
                raise PrivateKeyError('Windows only supports 2048 bit DSA keys based on SHA2 - this key is 2048 bits and based on SHA1, a non-standard combination that is usually generated by old versions of OpenSSL')

        if algo != 'ec':
            bit_size = key_object.bit_size

        alg_selector = key_object.curve[1] if algo == 'ec' else algo
        alg_constant = {
            'rsa': bcrypt_const.BCRYPT_RSA_ALGORITHM,
            'dsa': bcrypt_const.BCRYPT_DSA_ALGORITHM,
            'secp256r1': bcrypt_const.BCRYPT_ECDSA_P256_ALGORITHM,
            'secp384r1': bcrypt_const.BCRYPT_ECDSA_P384_ALGORITHM,
            'secp521r1': bcrypt_const.BCRYPT_ECDSA_P521_ALGORITHM,
        }[alg_selector]
        alg_handle = open_alg_handle(alg_constant)

        if algo == 'rsa':
            if key_type == 'public':
                blob_type = bcrypt_const.BCRYPT_RSAPUBLIC_BLOB
                magic = bcrypt_const.BCRYPT_RSAPUBLIC_MAGIC
                parsed_key = key_object['public_key'].parsed
                prime1_size = 0
                prime2_size = 0
            else:
                blob_type = bcrypt_const.BCRYPT_RSAFULLPRIVATE_BLOB
                magic = bcrypt_const.BCRYPT_RSAFULLPRIVATE_MAGIC
                parsed_key = key_object['private_key'].parsed
                prime1 = int_to_bytes(parsed_key['prime1'].native)
                prime2 = int_to_bytes(parsed_key['prime2'].native)
                exponent1 = int_to_bytes(parsed_key['exponent1'].native)
                exponent2 = int_to_bytes(parsed_key['exponent2'].native)
                coefficient = int_to_bytes(parsed_key['coefficient'].native)
                private_exponent = int_to_bytes(parsed_key['private_exponent'].native)
                prime1_size = len(prime1)
                prime2_size = len(prime2)

            public_exponent = int_to_bytes(parsed_key['public_exponent'].native)
            modulus = int_to_bytes(parsed_key['modulus'].native)

            blob_struct_pointer = struct(bcrypt, 'BCRYPT_RSAKEY_BLOB')
            blob_struct = unwrap(blob_struct_pointer)
            blob_struct.Magic = magic
            blob_struct.BitLength = key_object.bit_size
            blob_struct.cbPublicExp = len(public_exponent)
            blob_struct.cbModulus = len(modulus)
            blob_struct.cbPrime1 = prime1_size
            blob_struct.cbPrime2 = prime2_size

            blob = struct_bytes(blob_struct_pointer) + public_exponent + modulus
            if key_type == 'private':
                blob += prime1 + prime2
                blob += fill_width(exponent1, prime1_size)
                blob += fill_width(exponent2, prime2_size)
                blob += fill_width(coefficient, prime1_size)
                blob += fill_width(private_exponent, len(modulus))

        elif algo == 'dsa':
            if key_type == 'public':
                blob_type = bcrypt_const.BCRYPT_DSA_PUBLIC_BLOB
                public_key = key_object['public_key'].parsed.native
                params = key_object['algorithm']['parameters']
            else:
                blob_type = bcrypt_const.BCRYPT_DSA_PRIVATE_BLOB
                public_key = key_object.public_key.native
                private_bytes = int_to_bytes(key_object['private_key'].parsed.native)
                params = key_object['private_key_algorithm']['parameters']

            public_bytes = int_to_bytes(public_key)
            p = int_to_bytes(params['p'].native)
            g = int_to_bytes(params['g'].native)
            q = int_to_bytes(params['q'].native)

            q_len = len(q)

            key_width = max(len(public_bytes), len(g), len(p))

            public_bytes = fill_width(public_bytes, key_width)
            p = fill_width(p, key_width)
            g = fill_width(g, key_width)
            # We don't know the count or seed, so we set them to the max value
            # since setting them to 0 results in a parameter error
            count = b'\xff' * 4
            seed = b'\xff' * q_len

            if key_object.bit_size > 1024:
                if key_type == 'public':
                    magic = bcrypt_const.BCRYPT_DSA_PUBLIC_MAGIC_V2
                else:
                    magic = bcrypt_const.BCRYPT_DSA_PRIVATE_MAGIC_V2

                blob_struct_pointer = struct(bcrypt, 'BCRYPT_DSA_KEY_BLOB_V2')
                blob_struct = unwrap(blob_struct_pointer)
                blob_struct.dwMagic = magic
                blob_struct.cbKey = key_width
                # We don't know if SHA256 was used here, but the output is long
                # enough for the generation of q for the supported 2048/224,
                # 2048/256 and 3072/256 FIPS approved pairs
                blob_struct.hashAlgorithm = bcrypt_const.DSA_HASH_ALGORITHM_SHA256
                blob_struct.standardVersion = bcrypt_const.DSA_FIPS186_3
                blob_struct.cbSeedLength = q_len
                blob_struct.cbGroupSize = q_len
                blob_struct.Count = count

                blob = struct_bytes(blob_struct_pointer)
                blob += seed + q + p + g + public_bytes
                if key_type == 'private':
                    blob += private_bytes

            else:
                if key_type == 'public':
                    magic = bcrypt_const.BCRYPT_DSA_PUBLIC_MAGIC
                else:
                    magic = bcrypt_const.BCRYPT_DSA_PRIVATE_MAGIC

                blob_struct_pointer = struct(bcrypt, 'BCRYPT_DSA_KEY_BLOB')
                blob_struct = unwrap(blob_struct_pointer)
                blob_struct.dwMagic = magic
                blob_struct.cbKey = key_width
                blob_struct.Count = count
                blob_struct.Seed = seed
                blob_struct.q = q

                blob = struct_bytes(blob_struct_pointer) + p + g + public_bytes
                if key_type == 'private':
                    blob += private_bytes

        elif algo == 'ec':
            if key_type == 'public':
                blob_type = bcrypt_const.BCRYPT_ECCPUBLIC_BLOB
                public_key = key_object['public_key']
            else:
                blob_type = bcrypt_const.BCRYPT_ECCPRIVATE_BLOB
                public_key = key_object.public_key
                private_bytes = int_to_bytes(key_object['private_key'].parsed['private_key'].native)

            blob_struct_pointer = struct(bcrypt, 'BCRYPT_ECCKEY_BLOB')
            blob_struct = unwrap(blob_struct_pointer)

            magic = {
                ('public', 'secp256r1'): bcrypt_const.BCRYPT_ECDSA_PUBLIC_P256_MAGIC,
                ('public', 'secp384r1'): bcrypt_const.BCRYPT_ECDSA_PUBLIC_P384_MAGIC,
                ('public', 'secp521r1'): bcrypt_const.BCRYPT_ECDSA_PUBLIC_P521_MAGIC,
                ('private', 'secp256r1'): bcrypt_const.BCRYPT_ECDSA_PRIVATE_P256_MAGIC,
                ('private', 'secp384r1'): bcrypt_const.BCRYPT_ECDSA_PRIVATE_P384_MAGIC,
                ('private', 'secp521r1'): bcrypt_const.BCRYPT_ECDSA_PRIVATE_P521_MAGIC,
            }[(key_type, curve_name)]

            x, y = _decompose_ec_public_key(public_key)

            x_bytes = int_to_bytes(x)
            y_bytes = int_to_bytes(y)

            key_width = max(len(x_bytes), len(y_bytes))

            x_bytes = fill_width(x_bytes, key_width)
            y_bytes = fill_width(y_bytes, key_width)

            blob_struct.dwMagic = magic
            blob_struct.cbKey = key_width

            blob = struct_bytes(blob_struct_pointer) + x_bytes + y_bytes
            if key_type == 'private':
                blob += private_bytes

        key_handle_pointer = new(bcrypt, 'BCRYPT_KEY_HANDLE *')
        res = bcrypt.BCryptImportKeyPair(alg_handle, null(), blob_type, key_handle_pointer, blob, len(blob), bcrypt_const.BCRYPT_NO_KEY_VALIDATION)
        handle_error(res)

        key_handle = unwrap(key_handle_pointer)
        return container(key_handle, algo, curve_name, bit_size)

    finally:
        if alg_handle:
            close_alg_handle(alg_handle)


def _decompose_ec_public_key(octet_string):
    """
    Takes the ECPoint representation of an elliptic curve public key and
    decomposes it into the integers x and y

    :param octet_string:
        An asn1crypto.core.OctetString object containing the ECPoint value

    :return:
        A 2-element tuple of integers (x, y)
    """

    data = octet_string.native
    first_byte = data[0:1]

    # Uncompressed
    if first_byte == b'\x04':
        remaining = data[1:]
        field_len = len(remaining) // 2
        x = int_from_bytes(remaining[0:field_len])
        y = int_from_bytes(remaining[field_len:])
        return (x, y)

    if first_byte not in (b'\x02', b'\x03'):
        raise ValueError('Invalid EC public key - first byte is incorrect')

    raise ValueError('Compressed representations of EC public keys are not supported due to patent US6252960')


def load_private_key(source, source_type, password=None):
    """
    Loads a private key into a format usable with signing functions

    :param source:
        A byte string of file contents or a unicode string filename

    :param source_type:
        A unicode string describing the source - "file" or "bytes"

    :param password:
        A byte or unicode string to decrypt the PKCS12 file. Unicode strings will be encoded using UTF-8.

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A PrivateKey object
    """

    if source_type not in ('file', 'bytes'):
        raise ValueError('source_type is not one of "file" or "bytes"')

    if password is not None:
        if isinstance(password, str_cls):
            password = password.encode('utf-8')
        if not isinstance(password, byte_cls):
            raise ValueError('password is not a byte string')

    if source_type == 'file':
        with open(source, 'rb') as f:
            source = f.read()

    elif not isinstance(source, byte_cls):
        raise ValueError('source is not a byte string')

    private_object, algo = parse_private(source, password)

    return _load_key(private_object, algo, PrivateKey)


def load_public_key(source, source_type):
    """
    Loads a public key into a format usable with verify functions

    :param source:
        A byte string of file contents or a unicode string filename

    :param source_type:
        A unicode string describing the source - "file" or "bytes"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A PublicKey object
    """

    if source_type not in ('file', 'bytes'):
        raise ValueError('source_type is not one of "file" or "bytes"')

    if source_type == 'file':
        with open(source, 'rb') as f:
            source = f.read()

    elif not isinstance(source, byte_cls):
        raise ValueError('source is not a byte string')

    public_key, algo = parse_public(source)

    return _load_key(public_key, algo, PublicKey)


def load_pkcs12(source, source_type, password=None):
    """
    Loads a .p12 or .pfx file into a key and one or more certificates

    :param source:
        A byte string of file contents or a unicode string filename

    :param source_type:
        A unicode string describing the source - "file" or "bytes"

    :param password:
        A byte or unicode string to decrypt the PKCS12 file. Unicode strings will be encoded using UTF-8.

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A three-element tuple containing (PrivateKey, Certificate, [Certificate, ...])
    """

    if source_type not in ('file', 'bytes'):
        raise ValueError('source_type is not one of "file" or "bytes"')

    if password is not None:
        if isinstance(password, str_cls):
            password = password.encode('utf-8')
        if not isinstance(password, byte_cls):
            raise ValueError('password is not a byte string')

    if source_type == 'file':
        with open(source, 'rb') as f:
            source = f.read()

    elif not isinstance(source, byte_cls):
        raise ValueError('source is not a byte string')

    key_info, cert_info, extra_certs_info = parse_pkcs12(source, password)

    key = None
    cert = None

    if key_info:
        key = _load_key(key_info[0], key_info[1], PrivateKey)

    if cert_info:
        cert = _load_key(cert_info[0]['tbs_certificate']['subject_public_key_info'], cert_info[1], Certificate)

    extra_certs = [_load_key(info[0]['tbs_certificate']['subject_public_key_info'], info[1]) for info in extra_certs_info]

    return (key, cert, extra_certs)


def rsa_pkcs1v15_verify(certificate_or_public_key, signature, data, hash_algorithm):
    """
    Verifies an RSA, specifically RSASSA-PKCS-v1.5, signature

    :param certificate_or_public_key:
        A Certificate or PublicKey instance to verify the signature with

    :param signature:
        A byte string of the signature to verify

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework
        oscrypto.errors.SignatureError - when the signature is determined to be invalid
    """

    if certificate_or_public_key.algo != 'rsa':
        raise ValueError('The key specified is not an RSA public key')

    return _verify(certificate_or_public_key, signature, data, hash_algorithm)


def dsa_verify(certificate_or_public_key, signature, data, hash_algorithm):
    """
    Generates a DSA signature

    :param certificate_or_public_key:
        A Certificate or PublicKey instance to verify the signature with

    :param signature:
        A byte string of the signature to verify

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework
        oscrypto.errors.SignatureError - when the signature is determined to be invalid
    """

    if certificate_or_public_key.algo != 'dsa':
        raise ValueError('The key specified is not a DSA public key')

    return _verify(certificate_or_public_key, signature, data, hash_algorithm)


def ecdsa_verify(certificate_or_public_key, signature, data, hash_algorithm):
    """
    Generates an ECDSA signature

    :param certificate_or_public_key:
        A Certificate or PublicKey instance to verify the signature with

    :param signature:
        A byte string of the signature to verify

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework
        oscrypto.errors.SignatureError - when the signature is determined to be invalid
    """

    if certificate_or_public_key.algo != 'ec':
        raise ValueError('The key specified is not an EC public key')

    return _verify(certificate_or_public_key, signature, data, hash_algorithm)


def _verify(certificate_or_public_key, signature, data, hash_algorithm):
    """
    Verifies an RSA, DSA or ECDSA signature

    :param certificate_or_public_key:
        A Certificate or PublicKey instance to verify the signature with

    :param signature:
        A byte string of the signature to verify

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework
        oscrypto.errors.SignatureError - when the signature is determined to be invalid
    """

    if not isinstance(certificate_or_public_key, (Certificate, PublicKey)):
        raise ValueError('certificate_or_public_key is not an instance of the Certificate or PublicKey class')

    if not isinstance(signature, byte_cls):
        raise ValueError('signature is not a byte string')

    if not isinstance(data, byte_cls):
        raise ValueError('data is not a byte string')

    if hash_algorithm not in ('md5', 'sha1', 'sha256', 'sha384', 'sha512'):
        raise ValueError('hash_algorithm is not one of "md5", "sha1", "sha256", "sha384", "sha512"')

    hash_constant = {
        'md5': bcrypt_const.BCRYPT_MD5_ALGORITHM,
        'sha1': bcrypt_const.BCRYPT_SHA1_ALGORITHM,
        'sha256': bcrypt_const.BCRYPT_SHA256_ALGORITHM,
        'sha384': bcrypt_const.BCRYPT_SHA384_ALGORITHM,
        'sha512': bcrypt_const.BCRYPT_SHA512_ALGORITHM
    }[hash_algorithm]

    digest = getattr(hashlib, hash_algorithm)(data).digest()

    padding_info = null()
    flags = 0

    if certificate_or_public_key.algo == 'rsa':
        flags = bcrypt_const.BCRYPT_PAD_PKCS1
        padding_info_struct_pointer = struct(bcrypt, 'BCRYPT_PKCS1_PADDING_INFO')
        padding_info_struct = unwrap(padding_info_struct_pointer)
        # This has to be assigned to a variable to prevent cffi from gc'ing it
        hash_buffer = buffer_from_unicode(hash_constant)
        padding_info_struct.pszAlgId = cast(bcrypt, 'wchar_t *', hash_buffer)
        padding_info = cast(bcrypt, 'void *', padding_info_struct_pointer)
    else:
        # Bcrypt doesn't use the ASN.1 Sequence for DSA/ECDSA signatures,
        # so we have to convert it here for the verification to work
        signature = Signature.load(signature).to_bcrypt()

    res = bcrypt.BCryptVerifySignature(certificate_or_public_key.bcrypt_key_handle, padding_info, digest, len(digest), signature, len(signature), flags)
    if res == bcrypt_const.STATUS_INVALID_SIGNATURE:
        raise SignatureError('Signature is invalid')

    handle_error(res)


def rsa_pkcs1v15_sign(private_key, data, hash_algorithm):
    """
    Generates an RSA, specifically RSASSA-PKCS-v1.5, signature

    :param private_key:
        The PrivateKey to generate the signature with

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A byte string of the signature
    """

    if private_key.algo != 'rsa':
        raise ValueError('The key specified is not an RSA private key')

    return _sign(private_key, data, hash_algorithm)


def dsa_sign(private_key, data, hash_algorithm):
    """
    Generates a DSA signature

    :param private_key:
        The PrivateKey to generate the signature with

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A byte string of the signature
    """

    if private_key.algo != 'dsa':
        raise ValueError('The key specified is not a DSA private key')

    return _sign(private_key, data, hash_algorithm)


def ecdsa_sign(private_key, data, hash_algorithm):
    """
    Generates an ECDSA signature

    :param private_key:
        The PrivateKey to generate the signature with

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha224", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A byte string of the signature
    """

    if private_key.algo != 'ec':
        raise ValueError('The key specified is not an EC private key')

    return _sign(private_key, data, hash_algorithm)


def _sign(private_key, data, hash_algorithm):
    """
    Generates an RSA, DSA or ECDSA signature

    :param private_key:
        The PrivateKey to generate the signature with

    :param data:
        A byte string of the data the signature is for

    :param hash_algorithm:
        A unicode string of "md5", "sha1", "sha256", "sha384" or "sha512"

    :raises:
        ValueError - when any of the parameters are of the wrong type or value
        OSError - when an error is returned by the OS X Security Framework

    :return:
        A byte string of the signature
    """

    if not isinstance(private_key, PrivateKey):
        raise ValueError('private_key is not an instance of PrivateKey')

    if not isinstance(data, byte_cls):
        raise ValueError('data is not a byte string')

    if hash_algorithm not in ('md5', 'sha1', 'sha256', 'sha384', 'sha512'):
        raise ValueError('hash_algorithm is not one of "md5", "sha1", "sha256", "sha384", "sha512"')

    hash_constant = {
        'md5': bcrypt_const.BCRYPT_MD5_ALGORITHM,
        'sha1': bcrypt_const.BCRYPT_SHA1_ALGORITHM,
        'sha256': bcrypt_const.BCRYPT_SHA256_ALGORITHM,
        'sha384': bcrypt_const.BCRYPT_SHA384_ALGORITHM,
        'sha512': bcrypt_const.BCRYPT_SHA512_ALGORITHM
    }[hash_algorithm]

    digest = getattr(hashlib, hash_algorithm)(data).digest()

    padding_info = null()
    flags = 0

    if private_key.algo == 'rsa':
        flags = bcrypt_const.BCRYPT_PAD_PKCS1
        padding_info_struct_pointer = struct(bcrypt, 'BCRYPT_PKCS1_PADDING_INFO')
        padding_info_struct = unwrap(padding_info_struct_pointer)
        # This has to be assigned to a variable to prevent cffi from gc'ing it
        hash_buffer = buffer_from_unicode(hash_constant)
        padding_info_struct.pszAlgId = cast(bcrypt, 'wchar_t *', hash_buffer)
        padding_info = cast(bcrypt, 'void *', padding_info_struct_pointer)

    if private_key.algo == 'dsa' and private_key.bit_size > 1024 and hash_algorithm in ('md5', 'sha1'):
        raise ValueError('Windows does not support sha1 signatures with DSA keys based on sha224, sha256 or sha512')

    out_len = new(bcrypt, 'DWORD *')
    res = bcrypt.BCryptSignHash(private_key.bcrypt_key_handle, padding_info, digest, len(digest), null(), 0, out_len, flags)
    handle_error(res)

    buffer_len = deref(out_len)
    buffer = buffer_from_bytes(buffer_len)

    if private_key.algo == 'rsa':
        padding_info = cast(bcrypt, 'void *', padding_info_struct_pointer)

    res = bcrypt.BCryptSignHash(private_key.bcrypt_key_handle, padding_info, digest, len(digest), buffer, buffer_len, out_len, flags)
    handle_error(res)
    signature = bytes_from_buffer(buffer, deref(out_len))

    if private_key.algo != 'rsa':
        # Bcrypt doesn't use the ASN.1 Sequence for DSA/ECDSA signatures,
        # so we have to convert it here for the verification to work
        signature = Signature.from_bcrypt(signature).dump()

    return signature

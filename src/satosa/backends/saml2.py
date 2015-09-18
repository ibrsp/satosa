#!/usr/bin/env python
from base64 import b64encode, b64decode
import copy
import logging
from urllib.parse import urlparse

from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_HTTP_POST
from saml2.client_base import Base
from saml2.httputil import ServiceError
from saml2.config import SPConfig
from saml2.httputil import Unauthorized
from saml2.response import VerificationError
from saml2.s_utils import UnknownPrincipal
from saml2.s_utils import UnsupportedBinding
from satosa import VALID_ATTRIBUTES
from satosa.backends.base import BackendBase
from saml2.extension.ui import NAMESPACE as UI_NAMESPACE

from satosa.service import BINDING_MAP, response
import satosa.service as service

logger = logging.getLogger(__name__)


class SamlBackend(BackendBase):
    def __init__(self, outgoing, config):
        super(SamlBackend, self).__init__(outgoing)
        sp_config = SPConfig().load(copy.deepcopy(config), False)

        self.sp = Base(sp_config)
        self.idp_disco_query_param = "entityID"
        self.discosrv = True
        # if bindings:
        #     self.bindings = bindings
        # else:
        self.bindings = [BINDING_HTTP_REDIRECT, BINDING_HTTP_POST]
        logger.debug("--- SSO ---")

    def start_auth(self, context, request_info, state):
        _cli = self.sp
        req_args = request_info["req_args"]
        entity_id = context.internal_data["vopaas.target_entity_id"]
        entity_id = b64decode(entity_id).decode("utf-8")
        try:
            # Picks a binding to use for sending the Request to the IDP
            _binding, destination = _cli.pick_binding(
                "single_sign_on_service", self.bindings, "idpsso",
                entity_id=entity_id)
            logger.debug("binding: %s, destination: %s" % (_binding,
                                                           destination))
            # Binding here is the response binding that is which binding the
            # IDP should use to return the response.
            acs = _cli.config.getattr("endpoints", "sp")[
                "assertion_consumer_service"]
            # just pick one
            endp, return_binding = acs[0]
            req_id, req = _cli.create_authn_request(destination,
                                                    binding=return_binding,
                                                    **req_args)

            ht_args = _cli.apply_binding(_binding, "%s" % req, destination,
                                         relay_state=state)
            logger.debug("ht_args: %s" % ht_args)
        except Exception as exc:
            logger.exception(exc)
            return ServiceError("Failed to construct the AuthnRequest: %s" % exc)

        return response(_binding, ht_args)

    def authn_response(self, context, binding):
        """
        :param binding: Which binding the query came in over
        :returns: Error response or a response constructed by the transfer
            function
        """

        # _authn_response = unpack(environ, binding)
        _authn_response = context.request

        if not _authn_response["SAMLResponse"]:
            logger.info("Missing Response")
            return Unauthorized('Unknown user')

        binding = service.INV_BINDING_MAP[binding]
        try:
            _response = self.sp.parse_authn_request_response(
                _authn_response["SAMLResponse"], binding)
        except UnknownPrincipal as excp:
            logger.error("UnknownPrincipal: %s" % (excp,))
            return ServiceError("UnknownPrincipal: %s" % (excp,))
        except UnsupportedBinding as excp:
            logger.error("UnsupportedBinding: %s" % (excp,))
            return ServiceError("UnsupportedBinding: %s" % (excp,))
        except VerificationError as err:
            return ServiceError("Verification error: %s" % (err,))
        except Exception as err:
            return ServiceError("Other error: %s" % (err,))

        return self.auth_callback_func(context,
                                       self._translate_response(_response),
                                       _authn_response['RelayState'])

    def disco_response(self, context, *args):
        """
        If I got a useful response from the discovery server, continue with
        the authentication request.

        :return: redirect containing the authentication request
        """
        # info = self.unpack_redirect()
        info = context.request

        try:
            entity_id = info[self.idp_disco_query_param]
        except KeyError:
            resp = Unauthorized("You must chose an IdP")
            return resp(self.environ, self.start_response)
        else:
            # should I check the state variable ?
            return self.authn_request(entity_id, info["state"])

    def _translate_response(self, response):
        translated_response = {}
        translated_params = {}
        for param in VALID_ATTRIBUTES:
            try:
                translated_params[param] = response.ava[param]
            except KeyError:
                pass
        translated_response["ava"] = translated_params
        translated_response["name_id"] = response.get_subject()
        _authn_info = response.authn_info()[0]
        translated_response["auth_info"] = {"class_ref": _authn_info[0],
                                            "authn_auth": _authn_info[1][0]}
        return translated_response

    def register_endpoints(self):
        """
        Given the configuration, return a set of URL to function mappings.
        """

        url_map = []
        sp_endpoints = self.sp.config.getattr("endpoints", "sp")
        for endp, binding in sp_endpoints["assertion_consumer_service"]:
            p = urlparse(endp)
            url_map.append(("^%s?(.*)$" % p.path[1:], (self.authn_response,
                                                       BINDING_MAP[binding])))
            url_map.append(("^%s$" % p.path[1:], (self.authn_response,
                                                  BINDING_MAP[binding])))

        if self.discosrv:
            for endp, binding in sp_endpoints["discovery_response"]:
                p = urlparse(endp)
                url_map.append(("^%s$" % p.path[1:], (self.disco_response,
                                                      BINDING_MAP[binding])))

        return url_map
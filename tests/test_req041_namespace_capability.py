from __future__ import annotations

import unittest

from namespace_capability import (
    API_METHODS,
    namespace_capability_descriptor,
    negotiate_namespace_capability,
    validate_namespace_capability,
)
from tests.test_c1_livingmemory_decoupling import MemoryCompanionAdapterMixin


class _AdapterHost(MemoryCompanionAdapterMixin):
    pass


class NamespaceCapabilityTests(unittest.TestCase):
    def test_complete_descriptor_negotiates_ready(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        self.assertEqual([], validate_namespace_capability(descriptor))
        result = negotiate_namespace_capability(descriptor)
        self.assertTrue(result["available"])
        self.assertEqual("namespace_capability_ready", result["code"])

    def test_unbound_remote_is_well_formed_but_fails_closed(self) -> None:
        descriptor = namespace_capability_descriptor()
        self.assertEqual([], validate_namespace_capability(descriptor, require_available=False))
        result = negotiate_namespace_capability(descriptor)
        self.assertFalse(result["available"])
        self.assertEqual("namespace_capability_unavailable", result["code"])

    def test_missing_extra_and_mismatched_contract_are_rejected(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        descriptor.pop("context_fields")
        descriptor["unexpected"] = True
        descriptor["namespace_contract_fingerprint"] = "wrong"
        errors = validate_namespace_capability(descriptor)
        self.assertIn("namespace_capability_fields_invalid", errors)
        self.assertIn("namespace_contract_fingerprint_mismatch", errors)

    def test_available_requires_every_scoped_method(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS[:-1])
        self.assertFalse(descriptor["available"])
        self.assertIn("namespace_capability_unavailable", validate_namespace_capability(descriptor))

    def test_adapter_probe_handles_missing_exception_unavailable_and_ready(self) -> None:
        host = _AdapterHost()
        missing = host._memory_companion_probe_namespace_capabilities(object())
        self.assertEqual("namespace_capability_probe_missing", missing["code"])

        class Broken:
            @staticmethod
            def probe_namespace_context_capabilities():
                raise RuntimeError("remote failure must not escape")

        broken = host._memory_companion_probe_namespace_capabilities(Broken())
        self.assertEqual("namespace_capability_probe_exception", broken["code"])

        class Unbound:
            @staticmethod
            def probe_namespace_context_capabilities():
                return namespace_capability_descriptor()

        unavailable = host._memory_companion_probe_namespace_capabilities(Unbound())
        self.assertEqual("namespace_capability_unavailable", unavailable["code"])

        class Ready:
            @staticmethod
            def probe_namespace_context_capabilities():
                return namespace_capability_descriptor(available=True, methods=API_METHODS)

        ready = host._memory_companion_probe_namespace_capabilities(Ready())
        self.assertTrue(ready["available"])
        self.assertEqual("namespace_capability_ready", ready["code"])


if __name__ == "__main__":
    unittest.main()

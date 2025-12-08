#!/usr/bin/env python3
"""
Home Assistant Custom Component Validator
Validates custom components against Integration Quality Scale requirements.
"""

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import ast
import re


@dataclass
class ValidationResult:
    """Result of a validation check."""
    passed: bool
    message: str
    severity: str = "info"  # info, warning, error
    fix_suggestion: Optional[str] = None


@dataclass
class QualityReport:
    """Quality report for an integration."""
    domain: str
    tier_bronze: list[ValidationResult] = field(default_factory=list)
    tier_silver: list[ValidationResult] = field(default_factory=list)
    tier_gold: list[ValidationResult] = field(default_factory=list)
    tier_platinum: list[ValidationResult] = field(default_factory=list)
    file_issues: list[ValidationResult] = field(default_factory=list)
    
    def get_estimated_tier(self) -> str:
        """Estimate the current tier based on passing checks."""
        bronze_pass = all(r.passed for r in self.tier_bronze)
        silver_pass = all(r.passed for r in self.tier_silver)
        gold_pass = all(r.passed for r in self.tier_gold)
        platinum_pass = all(r.passed for r in self.tier_platinum)
        
        if platinum_pass and gold_pass and silver_pass and bronze_pass:
            return "🏆 Platinum"
        elif gold_pass and silver_pass and bronze_pass:
            return "🥇 Gold"
        elif silver_pass and bronze_pass:
            return "🥈 Silver"
        elif bronze_pass:
            return "🥉 Bronze"
        else:
            return "❓ No Score"


class HAComponentValidator:
    """Validator for Home Assistant custom components."""
    
    REQUIRED_MANIFEST_FIELDS = ["domain", "name", "version", "codeowners"]
    RECOMMENDED_MANIFEST_FIELDS = ["config_flow", "documentation", "iot_class", "requirements"]
    VALID_IOT_CLASSES = [
        "assumed_state", "calculated", "cloud_polling", "cloud_push",
        "local_polling", "local_push"
    ]
    
    def __init__(self, component_path: Path):
        self.path = component_path
        self.domain = component_path.name
        self.manifest: dict = {}
        self.report = QualityReport(domain=self.domain)
    
    def validate(self) -> QualityReport:
        """Run all validation checks."""
        self._validate_structure()
        self._load_manifest()
        self._validate_manifest()
        self._validate_config_flow()
        self._validate_init()
        self._validate_translations()
        self._validate_entities()
        self._validate_typing()
        return self.report
    
    def _validate_structure(self):
        """Validate file structure."""
        required_files = ["__init__.py", "manifest.json"]
        recommended_files = ["const.py", "config_flow.py", "strings.json"]
        
        for f in required_files:
            exists = (self.path / f).exists()
            self.report.file_issues.append(ValidationResult(
                passed=exists,
                message=f"Required file {f} {'exists' if exists else 'missing'}",
                severity="error" if not exists else "info",
                fix_suggestion=f"Create {f}" if not exists else None
            ))
        
        for f in recommended_files:
            exists = (self.path / f).exists()
            self.report.file_issues.append(ValidationResult(
                passed=exists,
                message=f"Recommended file {f} {'exists' if exists else 'missing'}",
                severity="warning" if not exists else "info",
                fix_suggestion=f"Create {f} for better quality score" if not exists else None
            ))
    
    def _load_manifest(self):
        """Load manifest.json."""
        manifest_path = self.path / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    self.manifest = json.load(f)
            except json.JSONDecodeError as e:
                self.report.file_issues.append(ValidationResult(
                    passed=False,
                    message=f"manifest.json is not valid JSON: {e}",
                    severity="error"
                ))
    
    def _validate_manifest(self):
        """Validate manifest.json contents."""
        # Required fields (Bronze)
        for field in self.REQUIRED_MANIFEST_FIELDS:
            exists = field in self.manifest
            self.report.tier_bronze.append(ValidationResult(
                passed=exists,
                message=f"Manifest field '{field}' {'present' if exists else 'missing'}",
                severity="error" if not exists else "info",
                fix_suggestion=f"Add '{field}' to manifest.json" if not exists else None
            ))
        
        # config_flow (Bronze)
        config_flow = self.manifest.get("config_flow", False)
        self.report.tier_bronze.append(ValidationResult(
            passed=config_flow is True,
            message=f"Config flow {'enabled' if config_flow else 'disabled'}",
            severity="error" if not config_flow else "info",
            fix_suggestion="Set 'config_flow': true in manifest.json" if not config_flow else None
        ))
        
        # codeowners (Silver)
        codeowners = self.manifest.get("codeowners", [])
        has_codeowners = len(codeowners) > 0 and all(c.startswith("@") for c in codeowners)
        self.report.tier_silver.append(ValidationResult(
            passed=has_codeowners,
            message=f"Code owners: {codeowners if codeowners else 'none defined'}",
            severity="warning" if not has_codeowners else "info",
            fix_suggestion="Add GitHub usernames to 'codeowners' (e.g., ['@username'])" if not has_codeowners else None
        ))
        
        # iot_class
        iot_class = self.manifest.get("iot_class")
        valid_iot = iot_class in self.VALID_IOT_CLASSES
        self.report.tier_bronze.append(ValidationResult(
            passed=valid_iot,
            message=f"IoT class: {iot_class}",
            severity="warning" if not valid_iot else "info",
            fix_suggestion=f"Set 'iot_class' to one of: {', '.join(self.VALID_IOT_CLASSES)}" if not valid_iot else None
        ))
        
        # Discovery (Gold)
        discovery_methods = ["zeroconf", "ssdp", "dhcp", "usb", "bluetooth", "homekit"]
        has_discovery = any(m in self.manifest for m in discovery_methods)
        self.report.tier_gold.append(ValidationResult(
            passed=has_discovery,
            message=f"Discovery {'configured' if has_discovery else 'not configured'}",
            severity="info",
            fix_suggestion="Consider adding discovery support for better UX" if not has_discovery else None
        ))
    
    def _validate_config_flow(self):
        """Validate config_flow.py."""
        config_flow_path = self.path / "config_flow.py"
        if not config_flow_path.exists():
            return
        
        content = config_flow_path.read_text()
        
        # Check for async_step_user (Bronze)
        has_step_user = "async_step_user" in content
        self.report.tier_bronze.append(ValidationResult(
            passed=has_step_user,
            message=f"User config step {'implemented' if has_step_user else 'missing'}",
            severity="error" if not has_step_user else "info"
        ))
        
        # Check for unique_id (Bronze)
        has_unique_id = "async_set_unique_id" in content
        self.report.tier_bronze.append(ValidationResult(
            passed=has_unique_id,
            message=f"Unique ID {'set' if has_unique_id else 'not set'}",
            severity="error" if not has_unique_id else "info",
            fix_suggestion="Call async_set_unique_id() to prevent duplicate entries" if not has_unique_id else None
        ))
        
        # Check for reauth flow (Silver)
        has_reauth = "async_step_reauth" in content
        self.report.tier_silver.append(ValidationResult(
            passed=has_reauth,
            message=f"Reauth flow {'implemented' if has_reauth else 'not implemented'}",
            severity="warning" if not has_reauth else "info",
            fix_suggestion="Implement async_step_reauth for better error recovery" if not has_reauth else None
        ))
        
        # Check for options flow (Gold)
        has_options = "OptionsFlow" in content or "async_get_options_flow" in content
        self.report.tier_gold.append(ValidationResult(
            passed=has_options,
            message=f"Options flow {'implemented' if has_options else 'not implemented'}",
            severity="info",
            fix_suggestion="Add OptionsFlow for reconfiguration support" if not has_options else None
        ))
    
    def _validate_init(self):
        """Validate __init__.py."""
        init_path = self.path / "__init__.py"
        if not init_path.exists():
            return
        
        content = init_path.read_text()
        
        # Check for async_setup_entry (Bronze)
        has_setup_entry = "async_setup_entry" in content
        self.report.tier_bronze.append(ValidationResult(
            passed=has_setup_entry,
            message=f"async_setup_entry {'implemented' if has_setup_entry else 'not implemented'}",
            severity="error" if not has_setup_entry else "info"
        ))
        
        # Check for async_unload_entry (Bronze)
        has_unload = "async_unload_entry" in content
        self.report.tier_bronze.append(ValidationResult(
            passed=has_unload,
            message=f"async_unload_entry {'implemented' if has_unload else 'not implemented'}",
            severity="warning" if not has_unload else "info"
        ))
        
        # Check for ConfigEntryNotReady (Silver)
        has_not_ready = "ConfigEntryNotReady" in content
        self.report.tier_silver.append(ValidationResult(
            passed=has_not_ready,
            message=f"ConfigEntryNotReady {'used' if has_not_ready else 'not used'}",
            severity="warning" if not has_not_ready else "info",
            fix_suggestion="Raise ConfigEntryNotReady when connection fails during setup" if not has_not_ready else None
        ))
        
        # Check for ConfigEntryAuthFailed (Silver)
        has_auth_failed = "ConfigEntryAuthFailed" in content
        self.report.tier_silver.append(ValidationResult(
            passed=has_auth_failed,
            message=f"ConfigEntryAuthFailed {'used' if has_auth_failed else 'not used'}",
            severity="warning" if not has_auth_failed else "info",
            fix_suggestion="Raise ConfigEntryAuthFailed when authentication fails" if not has_auth_failed else None
        ))
        
        # Check for DataUpdateCoordinator (Silver+)
        has_coordinator = "DataUpdateCoordinator" in content or "Coordinator" in content
        self.report.tier_silver.append(ValidationResult(
            passed=has_coordinator,
            message=f"DataUpdateCoordinator {'used' if has_coordinator else 'not used'}",
            severity="info",
            fix_suggestion="Use DataUpdateCoordinator for efficient data fetching" if not has_coordinator else None
        ))
        
        # Check for runtime_data pattern (Gold)
        has_runtime_data = "runtime_data" in content
        uses_hass_data = re.search(r"hass\.data\[DOMAIN\]", content) is not None
        
        self.report.tier_gold.append(ValidationResult(
            passed=has_runtime_data,
            message=f"Runtime data pattern {'used' if has_runtime_data else 'not used (using hass.data)' if uses_hass_data else 'not found'}",
            severity="info" if has_runtime_data else "warning",
            fix_suggestion="Use entry.runtime_data instead of hass.data[DOMAIN]" if uses_hass_data and not has_runtime_data else None
        ))
    
    def _validate_translations(self):
        """Validate translation files."""
        strings_path = self.path / "strings.json"
        translations_path = self.path / "translations"
        
        # Check strings.json exists (Gold)
        has_strings = strings_path.exists()
        self.report.tier_gold.append(ValidationResult(
            passed=has_strings,
            message=f"strings.json {'exists' if has_strings else 'missing'}",
            severity="warning" if not has_strings else "info",
            fix_suggestion="Create strings.json for translation support" if not has_strings else None
        ))
        
        # Check translations folder (Gold)
        has_translations = translations_path.exists() and (translations_path / "en.json").exists()
        self.report.tier_gold.append(ValidationResult(
            passed=has_translations,
            message=f"English translations {'exist' if has_translations else 'missing'}",
            severity="warning" if not has_translations else "info"
        ))
        
        if has_strings:
            try:
                with open(strings_path) as f:
                    strings = json.load(f)
                
                # Check for entity translations (Gold)
                has_entity_translations = "entity" in strings
                self.report.tier_gold.append(ValidationResult(
                    passed=has_entity_translations,
                    message=f"Entity translations {'defined' if has_entity_translations else 'not defined'}",
                    severity="info"
                ))
            except json.JSONDecodeError:
                pass
    
    def _validate_entities(self):
        """Validate entity implementations."""
        entity_files = list(self.path.glob("*.py"))
        
        for entity_file in entity_files:
            if entity_file.name in ["__init__.py", "config_flow.py", "const.py", "coordinator.py"]:
                continue
            
            content = entity_file.read_text()
            
            # Check for CoordinatorEntity (Silver+)
            if "Entity" in content and "Coordinator" in content:
                uses_coordinator_entity = "CoordinatorEntity" in content
                self.report.tier_silver.append(ValidationResult(
                    passed=uses_coordinator_entity,
                    message=f"{entity_file.name}: {'Uses' if uses_coordinator_entity else 'Does not use'} CoordinatorEntity",
                    severity="warning" if not uses_coordinator_entity else "info",
                    fix_suggestion=f"Extend CoordinatorEntity in {entity_file.name}" if not uses_coordinator_entity else None
                ))
            
            # Check for device_info (Silver)
            has_device_info = "device_info" in content or "DeviceInfo" in content
            if "Entity" in content:
                self.report.tier_silver.append(ValidationResult(
                    passed=has_device_info,
                    message=f"{entity_file.name}: device_info {'defined' if has_device_info else 'not defined'}",
                    severity="warning" if not has_device_info else "info"
                ))
            
            # Check for _attr_has_entity_name (Gold)
            has_entity_name = "_attr_has_entity_name" in content
            if "Entity" in content:
                self.report.tier_gold.append(ValidationResult(
                    passed=has_entity_name,
                    message=f"{entity_file.name}: _attr_has_entity_name {'set' if has_entity_name else 'not set'}",
                    severity="info"
                ))
            
            # Check for translation_key (Gold)
            has_translation_key = "_attr_translation_key" in content or "translation_key" in content
            if "Entity" in content:
                self.report.tier_gold.append(ValidationResult(
                    passed=has_translation_key,
                    message=f"{entity_file.name}: translation_key {'used' if has_translation_key else 'not used'}",
                    severity="info"
                ))
    
    def _validate_typing(self):
        """Validate type annotations (Platinum)."""
        py_files = list(self.path.glob("*.py"))
        
        for py_file in py_files:
            content = py_file.read_text()
            
            # Check for type hints
            has_type_hints = re.search(r"def \w+\([^)]*:[^)]+\)", content) is not None
            has_return_types = re.search(r"def \w+\([^)]*\)\s*->", content) is not None
            
            fully_typed = has_type_hints and has_return_types
            
            self.report.tier_platinum.append(ValidationResult(
                passed=fully_typed,
                message=f"{py_file.name}: {'Fully typed' if fully_typed else 'Missing type annotations'}",
                severity="info"
            ))


def generate_markdown_report(report: QualityReport) -> str:
    """Generate a markdown report."""
    lines = [
        f"# Home Assistant Integration Quality Report",
        f"**Component:** {report.domain}",
        f"**Estimated Tier:** {report.get_estimated_tier()}",
        "",
        "## File Structure",
        ""
    ]
    
    for result in report.file_issues:
        icon = "✅" if result.passed else "❌" if result.severity == "error" else "⚠️"
        lines.append(f"- {icon} {result.message}")
        if result.fix_suggestion:
            lines.append(f"  - 💡 {result.fix_suggestion}")
    
    lines.extend(["", "## Bronze Requirements", ""])
    for result in report.tier_bronze:
        icon = "✅" if result.passed else "❌"
        lines.append(f"- {icon} {result.message}")
        if result.fix_suggestion:
            lines.append(f"  - 💡 {result.fix_suggestion}")
    
    lines.extend(["", "## Silver Requirements", ""])
    for result in report.tier_silver:
        icon = "✅" if result.passed else "⚠️"
        lines.append(f"- {icon} {result.message}")
        if result.fix_suggestion:
            lines.append(f"  - 💡 {result.fix_suggestion}")
    
    lines.extend(["", "## Gold Requirements", ""])
    for result in report.tier_gold:
        icon = "✅" if result.passed else "⚠️"
        lines.append(f"- {icon} {result.message}")
        if result.fix_suggestion:
            lines.append(f"  - 💡 {result.fix_suggestion}")
    
    lines.extend(["", "## Platinum Requirements", ""])
    for result in report.tier_platinum:
        icon = "✅" if result.passed else "ℹ️"
        lines.append(f"- {icon} {result.message}")
    
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_component.py <path/to/custom_component>")
        sys.exit(1)
    
    component_path = Path(sys.argv[1])
    if not component_path.exists():
        print(f"Error: Path {component_path} does not exist")
        sys.exit(1)
    
    validator = HAComponentValidator(component_path)
    report = validator.validate()
    
    print(generate_markdown_report(report))


if __name__ == "__main__":
    main()

"""Render dependency-manager files from a deterministic manifest."""

from __future__ import annotations

import json
from typing import Any


def render_managed_artifact(path: str, manifest: dict[str, Any]) -> str | None:
    managed = manifest.get("managed_files") or {}
    owner = managed.get(path)
    if path == "pom.xml" and owner == "backend":
        return _render_pom(manifest.get("backend") or {})
    if path == "package.json" and owner == "frontend":
        return _render_package_json(manifest.get("frontend") or {})
    if path == "index.html" and owner == "frontend":
        return "<!doctype html>\n<html lang=\"zh-CN\">\n<head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>应用</title></head>\n<body><div id=\"app\"></div><script type=\"module\" src=\"/src/main.js\"></script></body>\n</html>\n"
    if path == "src/main.js" and owner == "frontend":
        return "import { createApp } from 'vue';\nimport App from './App.vue';\nimport './style.css';\n\ncreateApp(App).mount('#app');\n"
    if path == "src/main/java/com/example/app/Application.java" and owner == "backend":
        return (
            "package com.example.app;\n\n"
            "import org.springframework.boot.SpringApplication;\n"
            "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
            "@SpringBootApplication\npublic class Application {\n"
            "    public static void main(String[] args) {\n"
            "        SpringApplication.run(Application.class, args);\n"
            "    }\n}\n"
        )
    return None


def _render_pom(config: dict[str, Any]) -> str:
    dependencies = []
    for item in config.get("dependencies") or []:
        if not isinstance(item, dict) or not item.get("group") or not item.get("name"):
            continue
        scope = f"\n      <scope>{item['scope']}</scope>" if item.get("scope") else ""
        version = f"\n      <version>{item['version']}</version>" if item.get("version") else ""
        dependencies.append(
            "    <dependency>\n"
            f"      <groupId>{item['group']}</groupId>\n"
            f"      <artifactId>{item['name']}</artifactId>{version}{scope}\n"
            "    </dependency>"
        )
    java_version = str(config.get("java_version") or "17")
    spring_version = str(config.get("spring_boot_version") or "3.3.5")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{spring_version}</version>
    <relativePath/>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>agent-generated-app</artifactId>
  <version>0.0.1-SNAPSHOT</version>
  <properties><java.version>{java_version}</java.version></properties>
  <dependencies>
{chr(10).join(dependencies)}
  </dependencies>
  <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>
</project>
"""


def _render_package_json(config: dict[str, Any]) -> str:
    payload = {
        "name": "agent-generated-frontend",
        "private": True,
        "version": "0.0.1",
        "type": "module",
        "scripts": dict(config.get("scripts") or {}),
        "dependencies": dict(config.get("dependencies") or {}),
        "devDependencies": dict(config.get("dev_dependencies") or {}),
        "engines": {"node": str(config.get("node_version") or ">=18")},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

from app.workflow.maven_contract import normalize_h2_flyway_dependency


def dependency(name, version=""):
    return f"<dependency><groupId>org.flywaydb</groupId><artifactId>{name}</artifactId>{version}</dependency>"


def test_wrong_module_becomes_core_without_guessing_version():
    pom = f"<project>{dependency('flyway-database-h2')}</project>"
    fixed, changed = normalize_h2_flyway_dependency(pom)
    assert changed and "flyway-database-h2" not in fixed
    assert "flyway-core" in fixed and "<version>" not in fixed
    assert normalize_h2_flyway_dependency(fixed) == (fixed, False)


def test_existing_core_and_other_database_modules_are_preserved():
    core = dependency("flyway-core", "<version>10.20.1</version>")
    mysql = dependency("flyway-mysql")
    pom = f"<project xmlns='http://maven.apache.org/POM/4.0.0'>{core}{mysql}{dependency('flyway-database-h2')}</project>"
    fixed, changed = normalize_h2_flyway_dependency(pom)
    assert changed and core in fixed and mysql in fixed
    assert fixed.count("<artifactId>flyway-core</artifactId>") == 1


def test_malformed_xml_and_unrelated_coordinates_are_not_rewritten():
    pom = "<project>" + dependency("flyway-database-h2")
    assert normalize_h2_flyway_dependency(pom) == (pom, False)


def test_comment_or_managed_version_does_not_replace_runtime_dependency():
    comment = f"<!--{dependency('flyway-core')}-->"
    managed = f"<dependencyManagement><dependencies>{dependency('flyway-core')}</dependencies></dependencyManagement>"
    pom = f"<project>{comment}{managed}<dependencies>{dependency('flyway-database-h2')}</dependencies></project>"
    fixed, changed = normalize_h2_flyway_dependency(pom)
    assert changed and comment in fixed and managed in fixed
    assert fixed.count("<artifactId>flyway-core</artifactId>") == 3
    pom = "<project><dependency><groupId>custom.vendor</groupId><artifactId>flyway-database-h2</artifactId></dependency></project>"
    assert normalize_h2_flyway_dependency(pom) == (pom, False)

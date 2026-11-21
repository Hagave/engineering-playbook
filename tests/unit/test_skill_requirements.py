"""
Unit tests for the skill requirements of a write (playbook_core.required_skills)
and the skill gate that enforces them (context_guard._check_skill_gate).

The table lives in hooks/data/skill_requirements.json. Each layer is driven
here: companions on every source write, the language opt-in by extension, the
framework by the nearest manifest, and the topic by file name, import line or
code pattern, with a negative case for each trigger that could misfire.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import ClassVar

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import context_guard as cg  # noqa: E402
import playbook_core  # noqa: E402

_TABLE_PATH = os.path.join(_ROOT, 'hooks', 'data', 'skill_requirements.json')
with open(_TABLE_PATH, encoding='utf-8') as _f:
    _TABLE = json.load(_f)
_COMPANIONS = list(_TABLE['companions'])


class _ProjectTest(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='skillreq_')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _file(self, rel: str, content: str = '') -> str:
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    def _package_json(self, rel_dir: str = '', **sections: dict) -> None:
        self._file(os.path.join(rel_dir, 'package.json'), json.dumps({'name': 'app', **sections}))

    def _skills(self, rel: str, content: str = '', tool: str = 'Write') -> list[str]:
        path = os.path.join(self.dir, rel)
        tool_input = ({'file_path': path, 'content': content} if tool == 'Write'
                      else {'file_path': path, 'old_string': 'x', 'new_string': content})
        return [r.skill for r in playbook_core.required_skills(tool, tool_input, '', self.dir)]

    def _reasons(self, rel: str, content: str = '') -> dict[str, str]:
        path = os.path.join(self.dir, rel)
        reqs = playbook_core.required_skills('Write', {'file_path': path, 'content': content}, '', self.dir)
        return {r.skill: r.reason for r in reqs}


class TestCompanionsAndLanguage(_ProjectTest):

    def test_every_source_write_needs_the_companions(self) -> None:
        self.assertEqual(self._skills('src/a.sh', 'x=1'), _COMPANIONS)

    def test_each_language_extension_needs_its_opt_in(self) -> None:
        for language, row in _TABLE['languages'].items():
            for ext in row['extensions']:
                with self.subTest(language=language, ext=ext):
                    skills = self._skills(f'src/main{ext}', '')
                    self.assertEqual(skills[:len(_COMPANIONS)], _COMPANIONS)
                    self.assertIn(row['skill'], skills)

    def test_a_language_without_opt_in_needs_only_the_companions(self) -> None:
        for rel in ('main.c', 'core.h', 'index.php', 'run.sh', 'index.js'):
            with self.subTest(rel=rel):
                self.assertEqual(self._skills(rel, ''), _COMPANIONS)

    def test_every_source_extension_is_a_language_or_listed_as_uncovered(self) -> None:
        """A language is never dropped by omission: each extension the gate
        treats as source maps to an opt-in or is named in uncovered_extensions,
        and the two sets do not overlap."""
        covered = {ext for row in _TABLE['languages'].values() for ext in row['extensions']}
        uncovered = set(_TABLE['uncovered_extensions'])
        self.assertFalse(covered & uncovered)
        alternation = playbook_core._CODE_EXT.split('(?:', 1)[1].split(')', 1)[0]
        source = {ext for ext in alternation.split('|')}
        self.assertEqual({e.lstrip('.') for e in covered | uncovered}, source)

    def test_every_language_row_names_an_existing_skill_and_markers(self) -> None:
        for language, row in _TABLE['languages'].items():
            with self.subTest(language=language):
                self.assertTrue(row['markers'])
                self.assertTrue(os.path.isfile(os.path.join(
                    _ROOT, 'skills', row['skill'], 'SKILL.md')))

    def test_python_and_cpp_files_need_their_opt_in(self) -> None:
        self.assertIn('engineering-python', self._skills('tool.py', ''))
        self.assertIn('engineering-cpp', self._skills('core.cpp', ''))

    def test_the_file_decides_the_language_not_the_project_root(self) -> None:
        """A polyglot repository: a Python hook next to a tsconfig.json."""
        self._file('tsconfig.json', '{}')
        self.assertNotIn('engineering-typescript', self._skills('hooks/guard.py', 'x = 1'))

    def test_a_document_needs_nothing(self) -> None:
        self.assertEqual(self._skills('README.md', '# Title'), [])

    def test_the_reason_names_the_file(self) -> None:
        reasons = self._reasons('src/user.ts', '')
        self.assertEqual(reasons['engineering-typescript'], 'typescript file src/user.ts')
        self.assertEqual(reasons['engineering-security'], 'every source write')


class TestFrameworkByManifest(_ProjectTest):

    def test_nest_dependency_needs_the_nest_skill(self) -> None:
        self._package_json(dependencies={'@nestjs/core': '^11.0.0'})
        reasons = self._reasons('src/app.service.ts', '')
        self.assertEqual(reasons['engineering-nestjs'], '@nestjs/core in package.json')

    def test_a_dev_dependency_counts(self) -> None:
        self._package_json(devDependencies={'prisma': '^6.0.0'})
        self.assertIn('engineering-prisma', self._skills('src/repo.ts', ''))

    def test_the_name_outside_a_dependency_section_does_not_count(self) -> None:
        self._file('package.json', json.dumps({
            'name': 'app', 'description': 'not @nestjs/core', 'scripts': {'x': 'echo @nestjs/core'}}))
        self.assertNotIn('engineering-nestjs', self._skills('src/a.ts', ''))

    def test_the_nearest_manifest_governs_in_a_monorepo(self) -> None:
        self._package_json(dependencies={'react': '^19.0.0'})
        self._package_json('packages/api', dependencies={'@nestjs/core': '^11.0.0'})
        self._package_json('packages/web', dependencies={'react': '^19.0.0'})
        self.assertIn('engineering-nestjs', self._skills('packages/api/src/main.ts', ''))
        self.assertNotIn('engineering-nestjs', self._skills('packages/web/src/app.ts', ''))

    def test_a_framework_does_not_apply_to_another_language(self) -> None:
        self._package_json(dependencies={'@nestjs/core': '^11.0.0'})
        self.assertNotIn('engineering-nestjs', self._skills('tools/gen.go', ''))
        self.assertNotIn('engineering-nestjs', self._skills('scripts/seed.py', ''))

    def test_a_malformed_manifest_requires_no_framework(self) -> None:
        self._file('package.json', '{ not json')
        self.assertEqual(self._skills('src/a.ts', ''), _COMPANIONS + ['engineering-typescript'])


class TestTopicByPath(_ProjectTest):

    def test_docker_files_need_the_docker_skill_and_nothing_else(self) -> None:
        for rel in ('Dockerfile', 'docker/Dockerfile.prod', 'api.Dockerfile', 'compose.yaml',
                    'docker-compose.yml', 'docker-compose.test.yml', 'compose.override.yml',
                    '.dockerignore'):
            with self.subTest(rel=rel):
                self.assertEqual(self._skills(rel, 'FROM node'), ['engineering-docker'])

    def test_a_dockerfile_written_from_the_shell_needs_the_docker_skill(self) -> None:
        command = "cat > Dockerfile <<'EOF'\nFROM node:22\nEOF"
        reqs = playbook_core.required_skills(
            'Bash', {'command': command}, playbook_core.strip_heredoc_bodies(command), self.dir)
        self.assertEqual([r.skill for r in reqs], ['engineering-docker'])

    def test_a_dockerfile_written_through_tee_needs_the_docker_skill(self) -> None:
        command = "printf 'FROM node:22\\n' | tee Dockerfile"
        reqs = playbook_core.required_skills('Bash', {'command': command}, command, self.dir)
        self.assertEqual([r.skill for r in reqs], ['engineering-docker'])

    def test_a_source_file_copied_by_the_shell_needs_its_skills(self) -> None:
        self._file('templates/user.ts', 'export class User {}\n')
        command = 'cp templates/user.ts src/user.ts'
        reqs = playbook_core.required_skills('Bash', {'command': command}, command, self.dir)
        self.assertEqual([r.skill for r in reqs], _COMPANIONS + ['engineering-typescript'])

    def test_a_shell_redirect_to_a_log_needs_nothing(self) -> None:
        reqs = playbook_core.required_skills(
            'Bash', {'command': 'npm test > out.log 2>&1'}, 'npm test > out.log 2>&1', self.dir)
        self.assertEqual(reqs, [])

    def test_a_named_pipe_at_the_path_is_not_read(self) -> None:
        fifo = os.path.join(self.dir, 'src', 'pipe.ts')
        os.makedirs(os.path.dirname(fifo))
        os.mkfifo(fifo)
        self.assertIn('engineering-typescript', self._skills('src/pipe.ts', 'x'))

    def test_a_yaml_that_is_not_compose_needs_nothing(self) -> None:
        self.assertEqual(self._skills('.github/workflows/ci.yml', 'on: push'), [])

    def test_a_prisma_schema_needs_the_prisma_skill(self) -> None:
        self.assertEqual(self._skills('prisma/schema.prisma', 'model User {}'), ['engineering-prisma'])

    def test_test_files_need_the_testing_skill(self) -> None:
        for rel in ('test/unit/user.spec.ts', 'src/user.test.ts', 'pkg/user_test.go', 'test_user.py',
                    'tests/unit/helpers.py', 'src/test/java/UserTest.java', 'UserTests.cs',
                    'spec/user_spec.rb', 'src/__tests__/a.js'):
            with self.subTest(rel=rel):
                self.assertIn('engineering-testing', self._skills(rel, ''))

    def test_production_files_do_not_need_the_testing_skill(self) -> None:
        for rel in ('src/user.ts', 'src/contest.ts', 'internal/testdata_loader.go', 'src/attest.py'):
            with self.subTest(rel=rel):
                self.assertNotIn('engineering-testing', self._skills(rel, ''))

    def test_a_fixture_that_is_not_source_needs_nothing(self) -> None:
        self.assertEqual(self._skills('tests/fixtures/data.json', '{}'), [])


class TestTopicByImport(_ProjectTest):

    _RABBIT: ClassVar[dict[str, str]] = {
        'src/a.ts': "import * as amqp from 'amqplib';\n",
        'src/b.ts': "const amqp = require('amqplib/callback_api');\n",
        'cmd/c.go': 'package main\n\nimport (\n\t"fmt"\n\tamqp "github.com/rabbitmq/amqp091-go"\n)\n',
        'src/D.java': 'import com.rabbitmq.client.ConnectionFactory;\n',
        'src/E.cs': 'using RabbitMQ.Client;\n',
        'f.py': 'import pika\n',
        'g.rb': "require 'bunny'\n",
        'src/h.rs': 'use lapin::{Connection, ConnectionProperties};\n',
        'src/i.kt': 'import org.springframework.amqp.core.Queue\n',
    }

    def test_rabbitmq_imports_in_each_ecosystem(self) -> None:
        for rel, code in self._RABBIT.items():
            with self.subTest(rel=rel):
                self.assertIn('engineering-rabbitmq', self._skills(rel, code))

    def test_kafka_imports_in_each_ecosystem(self) -> None:
        cases = {
            'src/a.ts': "import { Kafka } from 'kafkajs';\n",
            'c.go': 'import "github.com/segmentio/kafka-go"\n',
            'src/D.java': 'import org.apache.kafka.clients.producer.KafkaProducer;\n',
            'E.cs': 'using Confluent.Kafka;\n',
            'f.py': 'from kafka import KafkaProducer\n',
            'g.py': 'from confluent_kafka import Producer\n',
            'src/h.ts': "const { Kafka } = await import('kafkajs');\n",
        }
        for rel, code in cases.items():
            with self.subTest(rel=rel):
                self.assertIn('engineering-kafka', self._skills(rel, code))

    def test_a_mention_outside_an_import_does_not_count(self) -> None:
        cases = {
            'src/a.ts': "// TODO: move to amqplib later\nconst lib = 'amqplib';\n",
            'b.py': 'BROKER = "pika"\n# import pika later\n',
            'c.go': 'package main\n\nvar name = "kafka"\n',
            'd.py': '"""\n    `./kafka` or `kafkajs` is not `kafka`; an unquoted import (Java, `pika`)\n"""\n',
        }
        for rel, code in cases.items():
            with self.subTest(rel=rel):
                skills = self._skills(rel, code)
                self.assertNotIn('engineering-rabbitmq', skills)
                self.assertNotIn('engineering-kafka', skills)

    def test_a_local_module_with_the_same_name_does_not_count(self) -> None:
        cases = {
            'src/a.ts': "import { send } from './kafka';\nimport { k } from '../kafka/producer';\n",
            'b.py': 'from app.kafka import publish\n',
            'src/c.ts': "import { x } from 'kafka-helpers';\n",
        }
        for rel, code in cases.items():
            with self.subTest(rel=rel):
                self.assertNotIn('engineering-kafka', self._skills(rel, code))

    def test_a_multi_edit_counts_every_new_string(self) -> None:
        path = os.path.join(self.dir, 'worker.py')
        edits = [{'old_string': 'a', 'new_string': 'x = 1'},
                 {'old_string': 'b', 'new_string': 'import pika'}]
        reqs = playbook_core.required_skills('MultiEdit', {'file_path': path, 'edits': edits}, '', self.dir)
        self.assertIn('engineering-rabbitmq', [r.skill for r in reqs])

    def test_an_edit_counts_the_imports_already_in_the_file(self) -> None:
        self._file('src/consumer.ts', "import * as amqp from 'amqplib';\n\nexport const x = 1;\n")
        self.assertIn('engineering-rabbitmq', self._skills('src/consumer.ts', 'export const x = 2;', tool='Edit'))

    def test_a_shell_heredoc_counts_what_it_writes(self) -> None:
        command = "cat > src/pub.ts <<'EOF'\nimport * as amqp from 'amqplib';\nEOF"
        reqs = playbook_core.required_skills(
            'Bash', {'command': command}, playbook_core.strip_heredoc_bodies(command), self.dir)
        skills = [r.skill for r in reqs]
        self.assertIn('engineering-typescript', skills)
        self.assertIn('engineering-rabbitmq', skills)


class TestNestTopics(_ProjectTest):

    def setUp(self) -> None:
        super().setUp()
        self._package_json(dependencies={'@nestjs/core': '^11.0.0', 'nestjs-pino': '^4.0.0'})

    def test_new_logger_needs_the_logger_skill(self) -> None:
        code = "export class CloseInvoices {\n  private readonly logger = new Logger(CloseInvoices.name);\n}\n"
        self.assertIn('engineering-nestjs-logger', self._skills('src/application/close.usecase.ts', code))

    def test_a_multi_line_nest_logger_import_needs_the_logger_skill(self) -> None:
        code = "import {\n  Injectable,\n  Logger,\n} from '@nestjs/common';\n"
        self.assertIn('engineering-nestjs-logger', self._skills('src/a.ts', code))

    def test_other_nest_common_imports_do_not_need_the_logger_skill(self) -> None:
        code = "import { Injectable, Inject } from '@nestjs/common';\n"
        self.assertNotIn('engineering-nestjs-logger', self._skills('src/a.ts', code))

    def test_the_bootstrap_needs_the_security_skill(self) -> None:
        code = "const app = await NestFactory.create<NestFastifyApplication>(AppModule);\n"
        self.assertIn('engineering-nestjs-security', self._skills('src/main.ts', code))

    def test_an_interceptor_needs_the_interceptors_skill(self) -> None:
        for code in ('export class TraceIdInterceptor implements NestInterceptor {}\n',
                     'export class T implements OnModuleInit, NestInterceptor<A, B> {}\n',
                     'export class HttpExceptionFilter implements ExceptionFilter {}\n'):
            with self.subTest(code=code):
                self.assertIn('engineering-nestjs-interceptors', self._skills('src/common/trace.ts', code))

    def test_nest_topics_need_the_nest_framework(self) -> None:
        other = tempfile.mkdtemp(prefix='skillreq_plain_')
        self.addCleanup(shutil.rmtree, other, True)
        path = os.path.join(other, 'src', 'log.ts')
        reqs = playbook_core.required_skills(
            'Write', {'file_path': path, 'content': 'const logger = new Logger("x");\n'}, '', other)
        self.assertNotIn('engineering-nestjs-logger', [r.skill for r in reqs])


class TestThrowawayAndFailure(_ProjectTest):

    def test_a_git_ignored_destination_needs_nothing(self) -> None:
        subprocess.run(['git', 'init', '-q', self.dir], check=True)
        self._file('.gitignore', 'scratch/\n')
        self.assertEqual(self._skills('scratch/Dockerfile', 'FROM x'), [])
        self.assertEqual(self._skills('scratch/probe.ts', 'x'), [])

    def test_an_unreadable_table_requires_nothing(self) -> None:
        saved = playbook_core._skill_requirements_cache
        self.addCleanup(setattr, playbook_core, '_skill_requirements_cache', saved)
        playbook_core._skill_requirements_cache = {}
        self.assertEqual(self._skills('src/a.ts', ''), [])


class TestSkillGateWithRequirements(unittest.TestCase):

    _REQ: ClassVar[list[playbook_core.SkillRequirement]] = [
        playbook_core.SkillRequirement('engineering-security', 'every source write'),
        playbook_core.SkillRequirement('engineering-docker', 'Dockerfile matches Dockerfile')]

    def test_all_loaded_passes(self) -> None:
        state = {'gov_loaded': True, 'skills': ['engineering-security', 'engineering-docker']}
        self.assertIsNone(cg._check_skill_gate(state, self._REQ))

    def test_missing_skills_are_named_with_their_reason(self) -> None:
        result = cg._check_skill_gate({'gov_loaded': True, 'skills': ['engineering-security']}, self._REQ)
        self.assertTrue(result['block'])
        self.assertIn('[playbook:skill-gate]', result['msg'])
        self.assertIn('engineering-docker (Dockerfile matches Dockerfile)', result['msg'])
        self.assertNotIn('engineering-security (', result['msg'])
        self.assertNotIn('engineering-governance has not', result['msg'])

    def test_governance_and_requirements_in_one_message(self) -> None:
        result = cg._check_skill_gate({}, self._REQ)
        self.assertIn('engineering-governance has not been loaded', result['msg'])
        self.assertIn('engineering-security (every source write)', result['msg'])

    def test_a_topic_file_that_is_not_source_does_not_need_governance(self) -> None:
        result = cg._check_skill_gate({}, self._REQ[1:], code_write=False)
        self.assertNotIn('engineering-governance', result['msg'])
        self.assertIn('engineering-docker', result['msg'])

    def test_a_loaded_skill_is_recorded_once(self) -> None:
        state: dict = {}
        cg._note_skill_loaded(state, 'engineering-docker')
        cg._note_skill_loaded(state, 'engineering-docker')
        cg._note_skill_loaded(state, 'playbook-config')
        self.assertEqual(state['skills'], ['engineering-docker'])


if __name__ == '__main__':
    unittest.main()

import json
import unittest

from wuwaru.billing import relay_coefficient
from wuwaru.providers import ContextProvider, TranslationItem
from wuwaru.validation import TranslationValidator, blocking_errors, tokens, validate


class PercentTests(unittest.TestCase):
    def test_percent_after_a_number_is_prose(self):
        self.assertEqual(tokens('At least 75% of pages, a 20% chance'), [])
        self.assertEqual(validate('Grants a 12% chance', 'Даёт шанс 12%'), [])

    def test_real_format_tokens_stay_frozen(self):
        self.assertEqual(tokens('Plain %s and %d value'), ['%s', '%d'])
        self.assertIn('TOKENS', validate('Plain %s value', 'Простое значение'))


class CombiningMarkTests(unittest.TestCase):
    def test_a_stress_mark_in_the_answer_is_flagged(self):
        self.assertIn('COMBINING_MARK', validate('Beringal left', 'Беринга́л ушёл'))

    def test_decomposed_cyrillic_and_marks_from_the_source_stay_allowed(self):
        self.assertNotIn('COMBINING_MARK', validate('Hero', 'Герой'))
        self.assertNotIn('COMBINING_MARK', validate('Zq́', 'Зе́'))


class GenderInBracketsTests(unittest.TestCase):
    def test_both_genders_in_brackets_are_flagged(self):
        for target in ('Я называл(а) имя', 'Я устал(а)', 'Я пришёл(ла) сюда', 'Я проснулся(ась)'):
            self.assertIn('GENDER_BRACKETS', validate('I said it', target), target)

    def test_ordinary_brackets_stay_allowed(self):
        self.assertNotIn('GENDER_BRACKETS', validate('Echo (rare)', 'Эхо (редкое)'))
        self.assertNotIn('GENDER_BRACKETS', validate('Value (a)', 'Значение (а)'))


class TranslatableTokenTests(unittest.TestCase):
    def test_values_may_change_when_the_keys_hold(self):
        for source, target in (('{Male=he;Female=she} left', '{Male=он;Female=она} ушла'),
                               ('{Cus:Ipt,Touch=Tap PC=Press Gamepad=Press} here', '{Cus:Ipt,Touch=Коснитесь PC=Нажмите Gamepad=Нажмите} здесь'),
                               ('<SapTag=0>{0}</SapTag> {Cus:Sap,S=day P=days SapTag=0} left', '<SapTag=0>{0}</SapTag> {Cus:Sap,S=дн. P=дн. SapTag=0} осталось'),
                               ('The <ano=Divine envoy>Echo</ano> rose', '<ano=Посланник>Эхо</ano> поднялось')):
            self.assertEqual(validate(source, target), [], target)

    def test_changed_structure_is_blocking(self):
        for source, target in (('{Male=he;Female=she} left', '{Male=он} ушла'),
                               ('{Male=he;Female=she} left', '{Male=;Female=она} ушла'),
                               ('{Cus:Sap,S=day P=days SapTag=0} left', '{Cus:Sap,S=дн. P=дн. SapTag=1} осталось'),
                               ('{Cus:Ipt,Touch=Tap PC=Press Gamepad=Press} go', '{Cus:Ipt,Touch=Жми PC=Жми} иди')):
            self.assertIn('TOKENS', blocking_errors(source, target), target)

    def test_english_left_inside_a_token_is_flagged_but_does_not_block(self):
        source, target = '{Male=he;Female=she} left', '{Male=he;Female=she} ушла'
        self.assertIn('TOKEN_VALUE', validate(source, target))
        self.assertEqual(blocking_errors(source, target), [])
        self.assertEqual({issue.severity for issue in TranslationValidator().check(source, target)}, {'WARNING'})

    def test_new_gender_token_is_not_accepted_yet(self):
        self.assertIn('TOKENS', validate('I went', '{Male=Я пошёл;Female=Я пошла}'))


class ContextProviderTests(unittest.TestCase):
    def items(self):
        context = lambda **fields: json.dumps(fields, ensure_ascii=False)
        return [TranslationItem('a' * 64, 'Did you see it?', context(scene=1, name='Мира', gender='F', kind='line'), {'Echo': 'Эхо'}),
                TranslationItem('b' * 64, 'I did.', context(scene=1, name='{PlayerName}', gender='F', kind='choice'), {'Echo': 'Эхо'}),
                TranslationItem('c' * 64, 'Confirm', context(kind='ui', field='Text'), {})]

    def provider(self, system='rules'):
        return ContextProvider('https://example.test/v1', 'gpt-6-luna', system=system, profile='RelayModels', key_getter=lambda: 'k')

    def test_the_address_to_the_player_goes_with_the_speaker_once(self):
        context = json.dumps(dict(scene=1, name='Мира', gender='F', address='ты', kind='line'), ensure_ascii=False)
        items = [TranslationItem('a' * 64, 'Hi', context, {}), TranslationItem('b' * 64, 'Bye', context, {})]
        sent = json.loads(self.provider().request_body(items)['messages'][1]['content'])
        self.assertEqual(sent['cast'], {'A': dict(name='Мира', gender='F', address='ты')})

    def test_request_shares_cast_and_glossary_and_uses_short_keys(self):
        body = self.provider().request_body(self.items())
        self.assertEqual(body['messages'][0], dict(role='system', content='rules'))
        sent = json.loads(body['messages'][1]['content'])
        self.assertEqual(sent['glossary'], {'Echo': 'Эхо'})
        self.assertEqual(sent['cast'], {'A': dict(name='Мира', gender='F'), 'B': dict(name='{PlayerName}', gender='F')})
        self.assertEqual(sent['lines'][0], dict(i='1', scene=1, who='A', kind='line', en='Did you see it?'))
        self.assertEqual(sent['lines'][2], dict(i='3', kind='ui', field='Text', en='Confirm'))
        self.assertNotIn('a' * 64, body['messages'][1]['content'])
        self.assertEqual(body['max_tokens'], self.provider().max_output_tokens)

    def test_answer_comes_back_under_the_engine_keys(self):
        provider = self.provider()
        answer = json.dumps(dict(translations=[dict(key=str(n), target='т') for n in (1, 2, 3)]))
        provider._request = lambda url, body=None: dict(choices=[dict(finish_reason='stop', message=dict(content=answer))])
        self.assertEqual(provider.translate(self.items(), allow_network=True), {'a' * 64: 'т', 'b' * 64: 'т', 'c' * 64: 'т'})

    def test_network_stays_opt_in_and_rules_change_the_identity(self):
        with self.assertRaises(PermissionError):
            self.provider().translate(self.items())
        self.assertNotEqual(self.provider('one').identity, self.provider('two').identity)


class CoefficientTests(unittest.TestCase):
    def test_coefficients_follow_the_users_plan_screen(self):
        self.assertEqual(relay_coefficient('gpt-6-luna'), 0.38)
        self.assertEqual(relay_coefficient('gpt-6-astra'), 10)
        self.assertIsNone(relay_coefficient('never-listed'))


if __name__ == '__main__':
    unittest.main()

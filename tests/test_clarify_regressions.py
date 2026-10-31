import unittest
from first_connection.clarify import budget_request, propose_constraints
from first_connection.engine import profile_from


class ClarifyRegressionTests(unittest.TestCase):
    def test_negated_budget_is_not_an_upper_bound_or_zero(self):
        for text in ['무료일 필요는 없어요.', '돈이 없는 건 아니에요.',
                     '3만원 이하일 필요는 없어요.', '3만원 이하가 아니에요.',
                     '0원 이하를 원하지 않아요.']:
            with self.subTest(text=text):self.assertIsNone(budget_request(text))

    def test_other_clause_negation_does_not_erase_an_explicit_limit(self):
        for text in ['무료일 필요는 없어요. 3만원 이하로 찾아주세요.',
                     '비용은 부담스럽지 않지만 25,000원까지 쓸 수 있어요.']:
            with self.subTest(text=text):self.assertIn(budget_request(text)[0],[30000,25000])

    def test_mixed_won_units_and_conflicting_limits(self):
        self.assertEqual(budget_request('2만 5천원 이하로 찾아주세요.')[0],25000)
        self.assertEqual(budget_request('10만 3천 원 이내로요.')[0],103000)
        self.assertIsNone(budget_request('3만원 이하나 5만원 이하 중 아직 정하지 못했어요.'))
        # A decimal must not become its last digit in ten-thousands.
        self.assertIsNone(budget_request('1.5만원 이하로 찾아주세요.'))

    def test_channel_exclusion_and_additive_expression_are_distinct(self):
        def proposals(text):return propose_constraints(profile_from({'region':'강남구','request':text}))
        self.assertEqual([r['value'] for r in proposals('전화 말고 온라인으로만 알아보고 싶어요.')],['phone'])
        self.assertEqual(proposals('전화 말고도 온라인이 가능해요.'),[])


if __name__=='__main__':unittest.main()

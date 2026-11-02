import json
import os
import tempfile
import unittest

import sms_parser
from sms_parser import (
    KIND_SIGNIN,
    KIND_UNKNOWN,
    build_patterns,
    extract_otp_code,
    load_patterns,
    parse_sms,
)

PATTERNS = load_patterns()


def patterns_with(**overrides):
    """The shipped patterns with one section replaced, for config tests."""
    with open(sms_parser.DEFAULT_PATTERNS_FILE, encoding="utf-8") as handle:
        data = json.load(handle)
    data.update(overrides)
    return build_patterns(data)


class OtpExtractionTests(unittest.TestCase):
    def test_number_words_with_any_separator(self):
        # IVAC's current TODAY format, the previous one, and the separators a
        # future change is most likely to use.
        for body in (
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            "IVAC verification code: ONE-TWO-THREE-FOUR-FIVE-SIX",
            "IVAC verification code: One Two Three Four Five Six",
            "IVAC verification code: ONE.TWO.THREE.FOUR.FIVE.SIX",
        ):
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), "123456")

    def test_number_words_mixed_with_digits(self):
        self.assertEqual(
            extract_otp_code("IVAC code ONE_2_THREE_FOUR_FIVE_SIX", PATTERNS),
            "123456",
        )

    def test_separated_digits(self):
        for body in (
            "IVAC code 1_2_3_4_5_6",
            "IVAC code 1-2-3-4-5-6",
            "IVAC code 1 2 3 4 5 6",
            "IVAC code 12 34 56",
        ):
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), "123456")

    def test_bengali_digits(self):
        self.assertEqual(extract_otp_code("IVAC code ১_২_৩_৪_৫_৬", PATTERNS), "123456")

    def test_full_width_digits(self):
        self.assertEqual(
            extract_otp_code("Your OTP is １２３４５６", PATTERNS),
            "123456",
        )

    def test_explicitly_separated_code_beats_an_unrelated_later_number(self):
        self.assertEqual(
            extract_otp_code("Ref 987654 amount 1_2_3_4_5_6", PATTERNS),
            "123456",
        )

    def test_code_is_read_from_after_the_otp_keyword(self):
        # The old "last 4-8 digit block" rule returned the balance here.
        self.assertEqual(
            extract_otp_code("Your OTP is 123456 and your balance is 15000", PATTERNS),
            "123456",
        )

    def test_date_after_the_code_is_not_glued_into_it(self):
        self.assertEqual(
            extract_otp_code("Your OTP is 123456. Offer valid till 12-10-2026", PATTERNS),
            "123456",
        )

    def test_phone_numbers_are_not_read_as_codes(self):
        self.assertEqual(
            extract_otp_code("OTP 123456 sent to 01712345678", PATTERNS),
            "123456",
        )
        self.assertEqual(
            extract_otp_code("You have received Tk 500 from 01712345678", PATTERNS),
            "",
        )

    def test_a_date_is_never_concatenated_into_an_eight_digit_code(self):
        self.assertNotEqual(
            extract_otp_code("Offer valid till 31-12-2026", PATTERNS),
            "31122026",
        )

    def test_digit_words_must_be_contiguous(self):
        self.assertEqual(
            extract_otp_code("one two three and four five six", PATTERNS),
            "",
        )

    def test_a_run_shorter_than_four_digits_is_not_a_code(self):
        self.assertEqual(extract_otp_code("code one two three", PATTERNS), "")

    def test_code_without_a_keyword_still_extracts(self):
        self.assertEqual(extract_otp_code("Use 998877 to log in", PATTERNS), "998877")

    def test_keyword_without_a_separator(self):
        self.assertEqual(extract_otp_code("bKash OTP123456", PATTERNS), "123456")
        self.assertEqual(extract_otp_code("IVAC_OTP_123456", PATTERNS), "123456")


class ClassificationTests(unittest.TestCase):
    def test_ivac_signin_otp(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.code, "123456")
        self.assertEqual(parsed.sender, "IVAC")

    def test_ivac_formats_all_route_to_signin(self):
        for body in (
            "IVAC login OTP: ONE-TWO-THREE-FOUR-FIVE-SIX",
            "IVAC login OTP: 1_2_3_4_5_6",
            "IVAC: your verification code is 1 2 3 4 5 6",
        ):
            with self.subTest(body=body):
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, "123456")

    def test_an_unknown_ivac_sender_number_does_not_change_routing(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            sender="8801999999999",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.sender, "IVAC")
        self.assertEqual(parsed.sender_number, "8801999999999")

    def test_marketing_message_is_unknown(self):
        parsed = parse_sms(
            "Get 50% cashback on your next purchase. Valid till 31-12-2026.",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_UNKNOWN)
        self.assertEqual(parsed.otp, "")
        self.assertEqual(parsed.code, "")
        self.assertTrue(parsed.reason)

    def test_otp_without_a_brand_defaults_to_ivac_signin(self):
        # Any message with an OTP sequence is routed to IVAC sign-in OTP even
        # if the sender is an unmapped gateway number and the body never names
        # IVAC.
        parsed = parse_sms("Your OTP is 123456", sender="09611123456", patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.sender, "IVAC")
        self.assertEqual(parsed.sender_number, "09611123456")

    def test_sender_map_resolves_a_body_without_a_brand(self):
        patterns = patterns_with(senders={"8801999999999": "IVAC"})
        parsed = parse_sms(
            "Your verification code is 123456",
            sender="+880 1999-999999",
            patterns=patterns,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.sender, "IVAC")

    def test_a_new_provider_needs_only_a_patterns_change(self):
        with open(sms_parser.DEFAULT_PATTERNS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        data["brands"].append({"label": "NewBank", "aliases": ["newbank"]})
        patterns = build_patterns(data)

        parsed = parse_sms("NewBank: Your OTP is 445566", sender="16247", patterns=patterns)
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "445566")
        self.assertEqual(parsed.sender, "NewBank")

    def test_unknown_sender_is_echoed_as_the_label(self):
        parsed = parse_sms("Win a prize today", sender="SomeBank", patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_UNKNOWN)
        self.assertEqual(parsed.sender, "SomeBank")
        self.assertEqual(parsed.sender_number, "SomeBank")

    def test_empty_message(self):
        parsed = parse_sms("", patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_UNKNOWN)
        self.assertEqual(parsed.code, "")
        self.assertEqual(parsed.otp, "")


class RealProviderTemplateTests(unittest.TestCase):
    """The exact bodies the providers send in production."""

    def test_wallet_otps_are_signin_otps_now(self):
        # Payment detection is gone, so a wallet OTP is delivered on the one
        # remaining channel rather than being dropped.
        cases = (
            "Your bKash OTP is 123456. Do not share with anyone.",
            "Nagad: Your OTP is 5 6 7 8 9 0",
            "DBBL Mobile Banking: OTP for your transaction is 482913",
            "Rocket: Your verification code is 998877",
        )
        expected = ("123456", "567890", "482913", "998877")
        for body, code in zip(cases, expected):
            with self.subTest(body=body):
                parsed = parse_sms(body, sender="16247", patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, code)

    def test_a_payment_receipt_is_not_a_signin_otp(self):
        parsed = parse_sms(
            "Payment of Tk 3,000.00 to DGEPAY SERVICES LIMITED-RM62457 is "
            "successful. Balance Tk 11,620.91. TrxID DJM4DNBFXE at 22/10/2026 14:02",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_UNKNOWN)
        self.assertEqual(parsed.otp, "")

    def test_ivac_signin_otp(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")

    def test_ivacbd_sequence_prompt_format(self):
        body = "(IVACBD) For security, type the following sequence when prompted 257000"
        parsed = parse_sms(body, patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "257000")
        self.assertEqual(parsed.code, "257000")
        self.assertEqual(parsed.sender, "IVAC")

    def test_ivacbd_sequence_prompt_with_sender(self):
        body = "(IVACBD) For security, type the following sequence when prompted 257000"
        parsed = parse_sms(body, sender="IVACBD", patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "257000")
        self.assertEqual(parsed.sender, "IVAC")

    def test_ivacbd_flexible_digit_lengths_4_to_8(self):
        lengths = (
            ("2570", "4-digit"),
            ("25701", "5-digit"),
            ("257000", "6-digit"),
            ("2570001", "7-digit"),
            ("25700012", "8-digit"),
        )
        for code, label in lengths:
            with self.subTest(label=label):
                body = f"(IVACBD) For security, type the following sequence when prompted {code}"
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, code)
                self.assertEqual(parsed.code, code)
                self.assertEqual(parsed.sender, "IVAC")


class ForwarderAddressTests(unittest.TestCase):
    """The forwarder posts the SMS sender address, so it has to identify brands."""

    def test_a_mapped_sender_labels_the_message(self):
        parsed = parse_sms(
            "Your verification code is 525926. Do not share it.",
            sender="+880 IVACBD",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "525926")
        self.assertEqual(parsed.sender, "IVAC")
        self.assertEqual(parsed.sender_number, "+880 IVACBD")

    def test_an_unmapped_sender_still_routes_a_brandless_otp(self):
        parsed = parse_sms(
            "Your security code is 525926. Do not share it.",
            sender="16216",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "525926")
        self.assertEqual(parsed.sender, "IVAC")

    def test_an_ivac_signin_otp_is_unaffected_by_the_sender(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            sender="8801999999999",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")


class SecurityWarningTests(unittest.TestCase):
    """Providers print a "never share your OTP" line of their own."""

    def test_a_code_inside_the_warning_is_still_read(self):
        parsed = parse_sms(
            "Do not share your OTP 445566 with anyone.",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "445566")

    def test_the_warning_does_not_win_over_the_real_otp_keyword(self):
        parsed = parse_sms(
            "Do NOT share your OTP or PIN with anyone. Your bKash OTP for PAYMENT "
            "of Tk.3,000.00 to IVAC (DGePay) is 319805. Expires in 2 min.",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "319805")


class PatternLoadingTests(unittest.TestCase):
    def test_shipped_patterns_only_cover_signin(self):
        self.assertEqual([brand.label for brand in PATTERNS.brands], ["IVAC"])

    def test_a_patterns_file_change_is_picked_up_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "patterns.json")
            with open(sms_parser.DEFAULT_PATTERNS_FILE, encoding="utf-8") as handle:
                data = json.load(handle)

            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            first = load_patterns(path)
            self.assertFalse(any(brand.label == "NewBank" for brand in first.brands))

            data["brands"].append({"label": "NewBank", "aliases": ["newbank"]})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            second = load_patterns(path)
            self.assertIsNot(first, second)
            self.assertTrue(any(brand.label == "NewBank" for brand in second.brands))

    def test_missing_file_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            load_patterns(os.path.join(tempfile.gettempdir(), "no-such-patterns.json"))

    def test_invalid_json_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "patterns.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(RuntimeError):
                load_patterns(path)

    def test_brand_without_aliases_is_rejected(self):
        with self.assertRaises(ValueError):
            patterns_with(brands=[{"label": "X", "aliases": []}])

    def test_otp_keywords_are_required(self):
        with self.assertRaises(ValueError):
            patterns_with(keywords={"signin": ["login"]})


class AllOtpFormatsTests(unittest.TestCase):
    """Verifies all 8 OTP formats detected by the app in order."""

    def test_1_worded_digits_zero_to_nine(self):
        cases = (
            ("Six-Zero-One-Seven-Eight-One", "601781"),
            ("Four_Zero_Three_Seven_Two_Four", "403724"),
            ("One/Two/Three/Four/Five/Six", "123456"),
            ("One, Two, Three, Four, Five, Six", "123456"),
            ("One:Two:Three:Four:Five:Six", "123456"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)
                self.assertEqual(parsed.sender, "IVAC")

    def test_2_labeled_otp(self):
        cases = (
            ("OTP is Ab76gTf", "Ab76gTf"),
            ("Your verification code is Ab76gTf", "Ab76gTf"),
            ("passcode: 987654", "987654"),
            ("pin is 4821", "4821"),
            ("password: AbCdEf", "AbCdEf"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_3_spaced_hyphen_digits(self):
        cases = (
            ("1 2 3 4 5 6", "123456"),
            ("1-2-3-4-5-6", "123456"),
            ("123-456", "123456"),
            ("12 34 56", "123456"),
            ("1234-5678", "12345678"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_4_alphanumeric(self):
        cases = (
            ("Ab76gTf", "Ab76gTf"),
            ("Your verification sequence Ab76gTf expires soon", "Ab76gTf"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_5_numeric(self):
        cases = (
            ("1234", "1234"),
            ("12345", "12345"),
            ("123456", "123456"),
            ("12345678", "12345678"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_6_compound_worded(self):
        cases = (
            ("Ten Eleven Eighty Six", "101186"),
            ("Twenty Three Forty Five Sixty Seven", "234567"),
            ("Twelve Thirty Four Fifty Six", "123456"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_7_alphabet_only(self):
        cases = (
            ("AbCdEfGh", "AbCdEfGh"),
            ("Your login code AbCdEfGh expires in 5 min", "AbCdEfGh"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_8_character_by_character_alphanumeric(self):
        cases = (
            ("Your IVAC verification code is A 1 g 0 G i 7", "A1g0Gi7"),
            ("Your IVAC verification code is A-1-g-0-G-i-7", "A1g0Gi7"),
            ("Your IVAC verification code is A_1_g_0_G_i_7", "A1g0Gi7"),
            ("IVAC code: 1 2 3 4 F g L", "1234FgL"),
            ("IVAC code: 1-2-3-4-F-g-L", "1234FgL"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)
                self.assertEqual(parsed.code, expected)
                self.assertEqual(parsed.sender, "IVAC")

    def test_8_grouped_alphanumeric(self):
        cases = (
            ("Your IVAC verification code is A1-g0-Gi7", "A1g0Gi7"),
            ("Your IVAC verification code is A1 g0 Gi7", "A1g0Gi7"),
            ("Your IVAC verification code is A1_g0_Gi7", "A1g0Gi7"),
            ("IVAC code: 12-34-A5", "1234A5"),
            ("IVAC code: AB12-CD3", "AB12CD3"),
            ("IVAC code: A1g0-Gi7Z", "A1g0Gi7Z"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_otp_code(body, PATTERNS), expected)
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)
                self.assertEqual(parsed.code, expected)

    def test_8_grouped_accepts_four_to_eight_characters(self):
        for body, expected in (
            ("IVAC code: A1-g0", "A1g0"),
            ("IVAC code: A1-g0-G", "A1g0G"),
            ("IVAC code: AB12-CD", "AB12CD"),
            ("IVAC code: A1g0-Gi7Z", "A1g0Gi7Z"),
        ):
            with self.subTest(body=body):
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_8_the_words_around_a_grouped_code_are_not_glued_to_it(self):
        parsed = parse_sms(
            "Your IVAC verification code is A1-g0-Gi7 and it expires in 5 minutes",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.otp, "A1g0Gi7")

    def test_8_character_by_character_accepts_four_to_eight_characters(self):
        for code in ("A 1 g 0", "A 1 g 0 G", "A 1 g 0 G i", "A 1 g 0 G i 7", "A 1 g 0 G i 7 Z"):
            with self.subTest(code=code):
                expected = code.replace(" ", "")
                body = f"Your IVAC verification code is {code}"
                parsed = parse_sms(body, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_SIGNIN)
                self.assertEqual(parsed.otp, expected)

    def test_8_a_separated_digit_run_is_not_truncated_at_the_first_letter(self):
        # The digit-span reader used to stop at "F" and report 1234.
        self.assertNotEqual(extract_otp_code("IVAC code: 1 2 3 4 F g L", PATTERNS), "1234")

    def test_8_words_never_form_a_character_run(self):
        # Only isolated single characters run together, so an ordinary sentence
        # cannot be read as a code by the new reader.
        self.assertEqual(extract_otp_code("Your code expires in 5 minutes", PATTERNS), "")
        self.assertEqual(extract_otp_code("A B C D", PATTERNS), "")
        self.assertNotEqual(extract_otp_code("IVAC code: A 1 g 0", PATTERNS), "A1g0A")


if __name__ == "__main__":
    unittest.main()

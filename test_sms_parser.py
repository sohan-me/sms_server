import json
import os
import tempfile
import unittest

import sms_parser
from sms_parser import (
    KIND_PAYMENT,
    KIND_SIGNIN,
    KIND_TRANSACTION,
    KIND_UNKNOWN,
    build_patterns,
    extract_amount,
    extract_otp_code,
    extract_trx_id,
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


class TrxExtractionTests(unittest.TestCase):
    def test_trx_id_after_each_provider_anchor(self):
        cases = (
            (
                "You have received Tk 500.00 from 01711111111. "
                "TrxID 9HABCDEFG1. Your new balance is Tk 1500.00.",
                "9HABCDEFG1",
            ),
            ("Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2", "7X9ABCDEF2"),
            ("DBBL Mobile Banking: Txn ID: 1234567890. Payment successful.", "1234567890"),
            ("Transaction ID is A1B2C3D4E5.", "A1B2C3D4E5"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_trx_id(body, PATTERNS), expected)

    def test_anchor_without_an_identifier_yields_nothing(self):
        self.assertEqual(
            extract_trx_id("Your transaction id has not been generated", PATTERNS),
            "",
        )

    def test_message_without_an_anchor_yields_nothing(self):
        self.assertEqual(extract_trx_id("Your OTP is 123456", PATTERNS), "")


class AmountExtractionTests(unittest.TestCase):
    def test_currency_anchored_amounts_are_normalised(self):
        cases = (
            ("You have received Tk 1,500.50 from 01711111111.", "1500.50"),
            ("Amount: BDT 250", "250.00"),
            ("Your new balance is Tk 1500", "1500.00"),
            ("Paid ৳ 300.00", "300.00"),
            # bKash writes the currency dot before the amount.
            ("Your bKash OTP for PAYMENT of Tk.3,000.00 to IVAC is 319805", "3000.00"),
            ("Payment of Tk. 1,234.50 to DGEPAY is successful", "1234.50"),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(extract_amount(body), expected)

    def test_message_without_an_amount(self):
        self.assertEqual(extract_amount("Your OTP is 123456"), "")


class ClassificationTests(unittest.TestCase):
    def test_ivac_signin_otp(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.code, "123456")
        self.assertEqual(parsed.trx, "")
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

    def test_payment_otps_by_wallet(self):
        cases = (
            ("Your bKash OTP is 123456. Do not share with anyone.", "bKash", "bKash", "123456"),
            ("Nagad: Your OTP is 5 6 7 8 9 0", "Nagad", "Nagad", "567890"),
            ("DBBL Mobile Banking: OTP for your transaction is 482913", "16247", "DBBL", "482913"),
            ("Rocket: Your verification code is 998877", "Rocket", "Rocket", "998877"),
        )
        for body, sender, label, code in cases:
            with self.subTest(body=body):
                parsed = parse_sms(body, sender=sender, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_PAYMENT)
                self.assertEqual(parsed.otp, code)
                self.assertEqual(parsed.sender, label)
                self.assertEqual(parsed.sender_number, sender)
                self.assertEqual(parsed.trx, "")

    def test_a_payment_brand_in_the_sender_is_enough(self):
        parsed = parse_sms(
            "Your OTP is 123456. Do not share it.",
            sender="bKash",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.sender, "bKash")

    def test_transaction_records(self):
        cases = (
            (
                "bKash",
                "You have received Tk 500.00 from 01711111111. "
                "TrxID 9HABCDEFG1. Your new balance is Tk 1500.00.",
                "9HABCDEFG1",
                "500.00",
            ),
            (
                "Nagad",
                "Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2",
                "7X9ABCDEF2",
                "250.00",
            ),
            (
                "DBBL",
                "DBBL Mobile Banking: Txn ID: 1234567890. Payment successful.",
                "1234567890",
                "",
            ),
        )
        for sender, body, trx, amount in cases:
            with self.subTest(body=body):
                parsed = parse_sms(body, sender=sender, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_TRANSACTION)
                self.assertEqual(parsed.trx, trx)
                self.assertEqual(parsed.code, trx)
                self.assertEqual(parsed.amount, amount)
                # An amount must never be offered as an OTP.
                self.assertEqual(parsed.otp, "")

    def test_otp_wins_when_a_message_carries_both(self):
        parsed = parse_sms(
            "Your bKash OTP is 123456 for TrxID 9HABCDEFG1",
            sender="bKash",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.trx, "")

    def test_marketing_message_is_unknown(self):
        parsed = parse_sms(
            "Get 50% cashback on your next purchase. Valid till 31-12-2026.",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_UNKNOWN)
        self.assertEqual(parsed.otp, "")
        self.assertEqual(parsed.trx, "")
        self.assertEqual(parsed.amount, "")
        self.assertTrue(parsed.reason)

    def test_otp_without_mfs_brand_defaults_to_ivac_signin(self):
        # When not from MFS, any message with an OTP sequence is routed to IVAC
        # sign-in OTP even if the sender is an unmapped gateway number and the
        # body does not name IVAC.
        parsed = parse_sms("Your OTP is 123456", sender="09611123456", patterns=PATTERNS)
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")
        self.assertEqual(parsed.sender, "IVAC")
        self.assertEqual(parsed.sender_number, "09611123456")

    def test_sender_map_resolves_a_body_without_a_brand(self):
        patterns = patterns_with(
            senders={"8801999999999": {"label": "IVAC", "family": KIND_SIGNIN}}
        )
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
        data["brands"].append(
            {"label": "NewBank", "family": KIND_PAYMENT, "aliases": ["newbank"]}
        )
        patterns = build_patterns(data)

        parsed = parse_sms("NewBank: Your OTP is 445566", sender="16247", patterns=patterns)
        self.assertEqual(parsed.kind, KIND_PAYMENT)
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


class RealProviderTemplateTests(unittest.TestCase):
    """The exact bodies the wallets and IVAC send in production."""

    def test_bkash_payment_otp_names_a_merchant_and_still_routes_to_payment(self):
        # The body says bKash *and* IVAC; IVAC is the payee, not the sender,
        # so proximity to the OTP keyword decides the channel.
        parsed = parse_sms(
            "Do NOT share your OTP or PIN with anyone. Your bKash OTP for PAYMENT "
            "of Tk.3,000.00 to IVAC (DGePay) is 319805. Expires in 2 min.",
            sender="bKash",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "319805")
        self.assertEqual(parsed.sender, "bKash")
        self.assertEqual(parsed.amount, "")

    def test_bkash_payment_otp_without_a_sender_hint(self):
        parsed = parse_sms(
            "Do NOT share your OTP or PIN with anyone. Your bKash OTP for PAYMENT "
            "of Tk.3,000.00 to IVAC (DGePay) is 319805. Expires in 2 min.",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "319805")

    def test_rocket_otp(self):
        parsed = parse_sms(
            "Rocket OTP:\n\nYour security code for Rocket transaction is 525926.",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "525926")
        self.assertEqual(parsed.sender, "Rocket")

    def test_rocket_transaction(self):
        parsed = parse_sms(
            "Tk4,500.00 paid through e-Commerce gateway. NetBal: Tk112,601.25 "
            "TxnId: 7028086216 Date:08-OCT-26 01:31:57 pm. "
            "Don't share your PIN with anybody",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_TRANSACTION)
        self.assertEqual(parsed.trx, "7028086216")
        self.assertEqual(parsed.amount, "4500.00")
        self.assertEqual(parsed.otp, "")

    def test_bkash_transaction(self):
        parsed = parse_sms(
            "Payment of Tk 3,000.00 to DGEPAY SERVICES LIMITED-RM62457 is "
            "successful. Balance Tk 11,620.91. TrxID DJM4DNBFXE at 22/10/2026 14:02",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_TRANSACTION)
        self.assertEqual(parsed.trx, "DJM4DNBFXE")
        self.assertEqual(parsed.amount, "3000.00")
        self.assertEqual(parsed.otp, "")

    def test_nagad_otp(self):
        parsed = parse_sms(
            "NEVER share your OTP or PIN with anyone. Nagad will never ask for "
            "these.\nYour OTP for Nagad ECOM payment is 035568.\n"
            "Validity: 2 minutes.\nHelpline: 16167",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "035568")
        self.assertEqual(parsed.sender, "Nagad")

    def test_nagad_transaction(self):
        parsed = parse_sms(
            "Payment to 'DGePAY Services Limite' is Successful.\n"
            "Amount: Tk 6000.0\nTxnID: 75PRV4ZK\nBalance: Tk 31840.01\n"
            "23/07/2026 16:57",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_TRANSACTION)
        self.assertEqual(parsed.trx, "75PRV4ZK")
        self.assertEqual(parsed.amount, "6000.00")
        self.assertEqual(parsed.otp, "")

    def test_ivac_signin_still_wins_over_no_wallet(self):
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

    def test_wallet_shortcodes_label_a_body_that_never_names_the_wallet(self):
        # The Rocket transaction template has no brand in it at all.
        cases = (
            (
                "16216",
                "Rocket",
                "Tk4,500.00 paid through e-Commerce gateway. NetBal: Tk112,601.25 "
                "TxnId: 7028086216 Date:08-OCT-26 01:31:57 pm. "
                "Don't share your PIN with anybody",
                "7028086216",
            ),
            (
                "16247",
                "bKash",
                "Payment of Tk 3,000.00 to DGEPAY SERVICES LIMITED-RM62457 is "
                "successful. Balance Tk 11,620.91. TrxID DJM4DNBFXE at 22/10/2026 14:02",
                "DJM4DNBFXE",
            ),
            (
                "16167",
                "Nagad",
                "Payment to 'DGePAY Services Limite' is Successful.\n"
                "Amount: Tk 6000.0\nTxnID: 75PRV4ZK\nBalance: Tk 31840.01\n"
                "23/07/2026 16:57",
                "75PRV4ZK",
            ),
        )
        for sender, label, body, trx in cases:
            with self.subTest(sender=sender):
                parsed = parse_sms(body, sender=sender, patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_TRANSACTION)
                self.assertEqual(parsed.trx, trx)
                self.assertEqual(parsed.sender, label)
                self.assertEqual(parsed.sender_number, sender)

    def test_a_shortcode_alone_routes_a_brandless_otp(self):
        parsed = parse_sms(
            "Your security code is 525926. Do not share it.",
            sender="16216",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "525926")
        self.assertEqual(parsed.sender, "Rocket")

    def test_the_same_shortcode_with_a_country_code_or_spaces(self):
        for sender in ("+880 16216", "88016216", " 16216 "):
            with self.subTest(sender=sender):
                parsed = parse_sms(
                    "Tk 100.00 paid. TxnId: 1234567890",
                    sender=sender,
                    patterns=PATTERNS,
                )
                self.assertEqual(parsed.kind, KIND_TRANSACTION)
                self.assertEqual(parsed.sender, "Rocket")

    def test_an_otp_naming_only_its_merchant_is_the_wallets_payment_otp(self):
        # IVAC is the payee here; the shortcode says the SMS came from bKash.
        parsed = parse_sms(
            "Your OTP for PAYMENT of Tk.3,000.00 to IVAC (DGePay) is 319805. "
            "Expires in 2 min.",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "319805")
        self.assertEqual(parsed.sender, "bKash")

    def test_an_ivac_signin_otp_is_unaffected_by_the_payee_rule(self):
        parsed = parse_sms(
            "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
            sender="8801999999999",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_SIGNIN)
        self.assertEqual(parsed.otp, "123456")


class SecurityWarningTests(unittest.TestCase):
    """Wallets print the same "never share your OTP" line on transactions."""

    def test_a_warning_does_not_turn_a_transaction_into_an_otp(self):
        for body in (
            "Never share your OTP with anyone. You received Tk 500.00. "
            "TrxID 9HABCDEFG1. Balance Tk 1500",
            "Never share your OTP with anyone. TrxID 9HABCDEFG1.",
            "You have received Tk 500.00 from 01711111111. TrxID 9HABCDEFG1. "
            "Never share your OTP or PIN with anyone.",
        ):
            with self.subTest(body=body):
                parsed = parse_sms(body, sender="16247", patterns=PATTERNS)
                self.assertEqual(parsed.kind, KIND_TRANSACTION)
                self.assertEqual(parsed.trx, "9HABCDEFG1")
                self.assertEqual(parsed.otp, "")

    def test_a_code_inside_the_warning_is_still_read(self):
        parsed = parse_sms(
            "Do not share your OTP 445566 with anyone.",
            sender="16247",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "445566")

    def test_the_warning_does_not_win_over_the_real_otp_keyword(self):
        parsed = parse_sms(
            "Do NOT share your OTP or PIN with anyone. Your bKash OTP for PAYMENT "
            "of Tk.3,000.00 to IVAC (DGePay) is 319805. Expires in 2 min.",
            patterns=PATTERNS,
        )
        self.assertEqual(parsed.kind, KIND_PAYMENT)
        self.assertEqual(parsed.otp, "319805")


class PatternLoadingTests(unittest.TestCase):
    def test_shipped_patterns_cover_signin_and_payment_brands(self):
        families = {brand.family for brand in PATTERNS.brands}
        self.assertEqual(families, {KIND_SIGNIN, KIND_PAYMENT})

    def test_a_patterns_file_change_is_picked_up_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "patterns.json")
            with open(sms_parser.DEFAULT_PATTERNS_FILE, encoding="utf-8") as handle:
                data = json.load(handle)

            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            first = load_patterns(path)
            self.assertFalse(any(brand.label == "NewBank" for brand in first.brands))

            data["brands"].append(
                {"label": "NewBank", "family": KIND_PAYMENT, "aliases": ["newbank"]}
            )
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

    def test_unknown_brand_family_is_rejected(self):
        with self.assertRaises(ValueError):
            patterns_with(brands=[{"label": "X", "family": "cheque", "aliases": ["x"]}])

    def test_otp_keywords_are_required(self):
        with self.assertRaises(ValueError):
            patterns_with(keywords={"signin": ["login"]})


class AllOtpFormatsTests(unittest.TestCase):
    """Verifies all 7 OTP formats detected by the app in order."""

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


if __name__ == "__main__":
    unittest.main()

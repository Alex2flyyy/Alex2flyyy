"""HTML analysis tests.

Fixtures represent the site archetypes this system actually encounters: the
2003 table-layout page, the mid-2010s WordPress build, the modern responsive
site, and the parked domain.
"""

from __future__ import annotations

from leadgen.evaluation.parser import parse_html

LEGACY_2003 = """
<html>
<head><title>Bob's Plumbing</title></head>
<body bgcolor="#FFFFFF">
<table width="800"><tr><td>
  <table><tr><td><font face="Arial" size="2"><b>BOB'S PLUMBING</b></font></td></tr></table>
  <center><marquee>Call today!</marquee></center>
  <img src="spacer.gif" width="1" height="1">
  <p>We do plumbing. Call 626-940-7551.</p>
  <p>&copy; 2003 Bob's Plumbing</p>
</td></tr></table>
</body></html>
"""

WORDPRESS_2016 = """
<html lang="en">
<head>
  <title>Pasadena Plumbing Services | Bob's Plumbing</title>
  <meta name="description" content="Licensed plumbers serving Pasadena and the San Gabriel Valley for over 30 years. Emergency service available.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.ico">
  <link rel="stylesheet" href="/wp-content/themes/x/style.css">
  <script src="/wp-includes/js/jquery/jquery.min.js"></script>
</head>
<body>
  <h1>Pasadena's Trusted Plumbers</h1>
  <a href="tel:+16269407551">Call (626) 940-7551</a>
  <a href="/contact">Contact Us</a>
  <a href="https://www.facebook.com/bobsplumbing">Facebook</a>
  <a class="btn" href="/quote">Get a Free Quote</a>
  <form action="/submit" method="post">
    <input type="text" name="name"><input type="email" name="email">
    <textarea name="message"></textarea><input type="submit" value="Send">
  </form>
  <img src="/truck.jpg" alt="Our service truck">
  <img src="/team.jpg">
  <p>%s</p>
  <p>&copy; 2016 Bob's Plumbing</p>
</body></html>
""" % ("Serving Pasadena since 1985. " * 60)

MODERN = """
<html lang="en">
<head>
  <title>Bob's Plumbing — Emergency Plumbers in Pasadena, CA</title>
  <meta name="description" content="24/7 emergency plumbing in Pasadena. Licensed, bonded, and insured with over 800 five-star reviews. Book online in sixty seconds.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta property="og:title" content="Bob's Plumbing">
  <link rel="icon" href="/favicon.svg">
  <script type="application/ld+json">{"@type":"Plumber","name":"Bob's Plumbing"}</script>
  <script src="/_next/static/chunks/main.js"></script>
</head>
<body>
  <h1>Emergency Plumbing in Pasadena</h1>
  <h2>Services</h2>
  <a href="tel:+16269407551">(626) 940-7551</a>
  <button>Book Now</button>
  <form><input type="email" name="email"><textarea name="msg"></textarea><button>Send</button></form>
  <a href="https://www.instagram.com/bobsplumbing">Instagram</a>
  <img src="/hero.webp" alt="Plumber at work">
  <script>window.dataLayer=[];</script>
  <script src="https://www.googletagmanager.com/gtag/js"></script>
  <p>%s</p>
  <footer>&copy; 2026 Bob's Plumbing</footer>
</body></html>
""" % ("Fast, honest plumbing service. " * 80)

PARKED = """
<html><head><title>bobsplumbing.com</title></head>
<body><h1>This domain is for sale</h1><p>Buy this domain today.</p></body></html>
"""


class TestLegacyDetection:
    def test_identifies_pre_2005_markup(self) -> None:
        page = parse_html(LEGACY_2003, "http://bobsplumbing.example")
        assert "font_tag" in page.legacy_markers
        assert "marquee" in page.legacy_markers
        assert "center_tag" in page.legacy_markers
        assert "spacer_gif" in page.legacy_markers
        assert page.design_era.startswith("pre-2005")
        assert page.modernity_score < 15

    def test_detects_table_layout(self) -> None:
        page = parse_html(LEGACY_2003, "http://x.example")
        assert page.uses_table_layout

    def test_no_viewport_meta(self) -> None:
        page = parse_html(LEGACY_2003, "http://x.example")
        assert not page.has_viewport_meta

    def test_reads_stale_copyright(self) -> None:
        page = parse_html(LEGACY_2003, "http://x.example")
        assert page.copyright_year == 2003

    def test_extracts_phone(self) -> None:
        page = parse_html(LEGACY_2003, "http://x.example")
        assert "+16269407551" in page.phones


class TestWordPressEra:
    def test_detects_platform(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert "WordPress" in page.tech_stack
        assert "jQuery" in page.tech_stack

    def test_finds_conversion_elements(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert page.has_contact_form
        assert page.has_click_to_call
        assert page.has_cta

    def test_finds_social_links(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert "facebook" in page.social_links

    def test_counts_missing_alt_text(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert page.total_images == 2
        assert page.images_missing_alt == 1

    def test_seo_basics_present(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert page.title
        assert page.meta_description
        assert page.h1_count == 1
        assert page.lang == "en"

    def test_dated_era_but_not_legacy(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert not page.uses_table_layout
        assert page.has_viewport_meta
        assert page.modernity_score > 30


class TestModernSite:
    def test_scores_high(self) -> None:
        page = parse_html(MODERN, "http://x.example")
        assert page.modernity_score > 65
        assert "current" in page.design_era or "recent" in page.design_era

    def test_detects_modern_signals(self) -> None:
        page = parse_html(MODERN, "http://x.example")
        assert page.has_open_graph
        assert page.structured_data_types
        assert page.has_analytics
        assert page.has_favicon
        assert "React" in page.tech_stack

    def test_content_is_substantial(self) -> None:
        page = parse_html(MODERN, "http://x.example")
        assert page.word_count > 300
        assert not page.is_minimal


class TestEdgeCases:
    def test_parked_domain_detected(self) -> None:
        page = parse_html(PARKED, "http://x.example")
        assert page.is_parked

    def test_empty_html(self) -> None:
        page = parse_html("", "http://x.example")
        assert page.is_minimal
        assert page.modernity_score == 0.0

    def test_malformed_html_does_not_raise(self) -> None:
        page = parse_html("<html><body><div><p>unclosed", "http://x.example")
        assert page.word_count >= 1

    def test_search_box_is_not_a_contact_form(self) -> None:
        html = '<html><body><form><input type="text" name="search"><input type="submit"></form></body></html>'
        assert not parse_html(html, "http://x.example").has_contact_form

    def test_finds_contact_page_link(self) -> None:
        page = parse_html(WORDPRESS_2016, "http://x.example")
        assert page.contact_page_url
        assert "contact" in page.contact_page_url

    def test_share_widgets_are_not_social_profiles(self) -> None:
        html = '<html><body><a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a></body></html>'
        assert "facebook" not in parse_html(html, "http://x.example").social_links

    def test_links_are_split_internal_external(self) -> None:
        html = (
            '<html><body><a href="/about">About</a>'
            '<a href="https://other.example/x">Other</a></body></html>'
        )
        page = parse_html(html, "https://mine.example/")
        assert any("mine.example" in link for link in page.internal_links)
        assert any("other.example" in link for link in page.external_links)

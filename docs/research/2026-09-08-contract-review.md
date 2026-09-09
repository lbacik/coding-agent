# Przegląd kontraktów coding-agent v1

Stan na 2026-09-08. Przegląd mapy [#1](https://github.com/lbacik/coding-agent/issues/1), rozstrzygnięć jej dzieci, lokalnego `CONTEXT.md` i ADR. To ocena specyfikacji, nie działającej implementacji. Poniżej oddzielono fakty wynikające z dokumentów od wniosków i proponowanych zmian. Nie zmieniano kodu ani GitHub Issues.

## Status i rozstrzygnięcia, których nie należy ponownie otwierać przez pomyłkę

Mapa dotyczy gotowej specyfikacji i planu realizacji. Nie obiecuje działającego Workera. Zostało [#8 — kolejność implementacji i scenariusze akceptacyjne](https://github.com/lbacik/coding-agent/issues/8). Lokalny projekt zawiera dokumentację, a nie implementację Workera.

Kilka starszych zdań w zamkniętych ticketach jest już nieaktualnych:

- Badanie [#3](https://github.com/lbacik/coding-agent/issues/3#issuecomment-5574438038) dotyczyło instalatora 0.5.1. [Rozstrzygnięcie #6](https://github.com/lbacik/coding-agent/issues/6#issuecomment-5587373466) sprawdziło wydane 0.6.0, wybrało `--only`/`--json`, instalację przy budowie obrazu i własną weryfikację kompletności. Brak selekcji z badania 0.5.1 nie jest dzisiejszym blokerem.
- Kolejność review/commit pozostawiona otwarta w [#2](https://github.com/lbacik/coding-agent/issues/2#issuecomment-5574437775) jest rozstrzygnięta przez [#7](https://github.com/lbacik/coding-agent/issues/7#issuecomment-5590399283) i [ADR 0002](../adr/0002-commit-and-push-before-review.md): commit i push przed walidacją oraz review. Stary diagram #5 z późniejszym pushem jest zastąpiony.
- `Checkpoint` w #4 oznacza obecnie `Gate`; `delivered-with-drift` to obecnie `delivered-as-draft` z przyczyną. Unassignment jest osobnym węzłem w sekwencji terminalnych zapisów, a nie tym samym wywołaniem co etykiety. Korekty są jawne w [#5](https://github.com/lbacik/coding-agent/issues/5#issuecomment-5582764617) oraz [#7 §13](https://github.com/lbacik/coding-agent/issues/7#issuecomment-5590399283).
- Dawne uwagi „CONTEXT.md uncommitted” nie opisują aktualnego drzewa: słownik i oba ADR są już w historii, ostatni commit `7741015`.

Architektura jednego sekwencyjnego Workera, jawnych zapisów, Run Ledger, przypiętego Skill Bundle i walidacji prowadzonej przez harness tworzy spójny kierunek. Warto zachować oddzielenie SQLite Run Ledger od frameworkowego checkpointera, ludzką kontrolę Selection Label oraz gałąź na Attempt. Nie znaleziono potrzeby zmiany LangGraph, dodania Postgresa ani ponownego otwierania [ADR 0001](../adr/0001-pinned-toolchain-matrix.md). Najważniejszy zewnętrzny bloker dotyczy wybranego sposobu uwierzytelniania; pozostałe ryzyka dotyczą szczegółów kontraktów na styku decyzji.

## Najpierw: konto techniczne jako collaborator nie wystarcza do wybranego fine-grained PAT

**Fakt.** [#9 §1–2 i §12](https://github.com/lbacik/coding-agent/issues/9#issuecomment-5589618211) wybiera osobne konto techniczne zaproszone do Target Repository oraz fine-grained PAT ograniczony do tego repozytorium. Aktualna [oficjalna dokumentacja ograniczeń tokenów](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations) wymienia zapis do repozytoriów, gdzie właściciel tokena jest outside lub repository collaborator, jako nieobsługiwany scenariusz fine-grained PAT.

**Wniosek.** Ustalenie #9 nie działa ogólnie w opisanej postaci. Szczególnie istotny przypadek to osobne konto techniczne zaproszone do repozytorium należącego do innego konta osobistego. Samo przyjęcie zaproszenia nie rozwiązuje ograniczenia tokena. Dla organizacji członkostwo konta technicznego w organizacji jest innym warunkiem niż zaproszenie jako outside collaborator.

**Propozycja.** Przed pierwszym wycinkiem implementacyjnym ustalić docelowy typ właściciela repozytorium i relację konta technicznego, a następnie potwierdzić dostęp dla tej rzeczywistej konfiguracji. Opcje do świadomego wyboru: konto będące członkiem organizacji i fine-grained PAT; classic PAT z jawnie szerszym zakresem; albo ponowne rozważenie wyłączonego z v1 GitHub App. Nie jest to automatyczna rekomendacja zmiany na App ani wykonanie migracji — jest to wykryta przesłanka do ponownego otwarcia konkretnego fragmentu #9.

## 1. Limit Clarification Rounds jest sprzeczny z resetem przy wznowieniu

**Fakt.** [#4 §6, §8, T3–T6 i scenariusz D](https://github.com/lbacik/coding-agent/issues/4#issuecomment-5580839920) ustala limit trzech Clarification Rounds na issue, ale każde ponowne nałożenie Selection Label zeruje licznik. Bez tej etykiety następna runda nie może się rozpocząć. Scenariusz D równocześnie mówi o resecie i o wzroście licznika z 1 do 2.

**Wniosek.** W literalnym wykonaniu tych reguł próg trzech rund jest nieosiągalny w zwykłym cyklu pytań i odpowiedzi. To nie autonomiczna pętla bez człowieka — ręczne nałożenie etykiety nadal jest konieczne — ale licznik nie zapewnia obiecanego ograniczenia.

**Propozycja.** Reautoryzacja podczas `awaiting-clarification` zachowuje liczbę rund; dopiero jawne rozpoczęcie pracy po terminalnym wyczerpaniu budżetu ją resetuje. Rozdzielić ten licznik od budżetu automatycznych ponowień. Test w #8: trzy niepełne odpowiedzi i trzy ponowne nałożenia etykiety muszą dojść do `ready-for-human`, a nie wrócić do pierwszej rundy.

## 2. Baseline Failure na poziomie całej komendy może ukryć regresję

**Fakt.** [#7 §3 i invariant 8](https://github.com/lbacik/coding-agent/issues/7#issuecomment-5590399283) wyłącza z blokowania komendę, która nie przechodzi również na Base Revision. Baseline jest sprawdzany dopiero po niepowodzeniu walidacji Delivery Snapshot.

**Wniosek.** Komenda uruchamiająca całą suite nie jest pojedynczym testem. Jeżeli na Base Revision pada test A, a na Delivery Snapshot A i nowy test B, oba uruchomienia mają kod niezerowy. Obecny opis pozwala wyłączyć całą komendę jako Baseline Failure, mimo nowej regresji B. Analogiczny problem dotyczy nowych błędów analizatora przy już czerwonej analizie bazowej.

**Propozycja.** Wyłączać wyłącznie porównywalne, istniejące wcześniej błędy na podstawie identyfikatorów testów/diagnostyk. Gdy narzędzie nie daje takiego dowodu, wynik nie powinien nazywać się zweryfikowanym brakiem regresji; bezpiecznym wynikiem pozostaje Delivery Draft. Zachować lazy baseline, jeśli porównanie potrafi dowieść braku nowego błędu.

**Do doprecyzowania w #8.** Minimalny format Validation Evidence dla pytest, PHPUnit i wybranego runnera TS: wykonane testy, pominięte testy, błędy kolekcji, exit code, identyfikatory błędów, komenda, Delivery Snapshot i konfiguracja środowiska. „Zebrane” lub „pominięte” nie znaczy „wykonane”. Nie każdy runner zachowuje się tak samo przy braku testów.

Osobną niewiadomą jest wersja Project Profile używana do walidacji: dokumenty mówią o własnych komendach projektu, lecz nie rozstrzygają wprost, czy kandydat może je zmienić przed własnym sprawdzeniem. Zalecenie: zapisać kontrakt walidacji z Base Revision i jawnie obsłużyć autoryzowane zmiany profilu; kandydat nie może uzyskać zielonego wyniku przez niejawne osłabienie sprawdzających go komend. To propozycja wymagania, nie wykryty błąd implementacji.

## 3. `author_association` nie dowodzi prawa zapisu

**Fakt.** [#9 §3](https://github.com/lbacik/coding-agent/issues/9#issuecomment-5589618211) interpretuje `OWNER`, `MEMBER`, `COLLABORATOR` jako człowieka z prawem zapisu. Oficjalny [enum CommentAuthorAssociation](https://docs.github.com/en/graphql/reference/issues#commentauthorassociation) opisuje relację autora z repozytorium lub organizacją, nie aktualną rolę uprawnień. `MEMBER` opisuje członkostwo organizacji; `COLLABORATOR` współpracę przy repozytorium.

**Wniosek.** Filtr może przyjąć odpowiedź osoby bez wymaganego prawa zapisu. Pierwszeństwo porównania numerycznego Agent Identity poprawnie wyklucza własne odpowiedzi i należy je zachować, ale nie naprawia uprawnień pozostałych autorów.

**Propozycja.** Po wykluczeniu siebie, autorów automatycznych i Stale Answers sprawdzić efektywne uprawnienia autora. GitHub ma do tego [Get repository permissions for a user](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user); endpoint wymaga dla tokena `Metadata: read`. Zmapować odpowiedź na przyjętą politykę „write lub wyżej”, z uwzględnieniem normalizacji ról przez API. Testy: członek organizacji z read/triage, collaborator z read, osoba z write, własny komentarz Workera oraz komentarz aplikacji.

## 4. Dokończenie Delivery po częściowym zapisie potrzebuje osobnego kontraktu

**Fakt.** [#5 §9](https://github.com/lbacik/coding-agent/issues/5#issuecomment-5582764617) kieruje wyczerpanie retry węzła do nowego Attempt. [#9 scenariusz E](https://github.com/lbacik/coding-agent/issues/9#issuecomment-5589618211) zakłada, że taki nowy Attempt odnajdzie PR po tej samej gałęzi. Późniejsze [#7 §2](https://github.com/lbacik/coding-agent/issues/7#issuecomment-5590399283) wprowadza osobną gałąź na każdy Attempt, a otwarty Delivery PR wyklucza issue z normalnej selekcji.

**Wniosek.** „Powtórz implementację jako nowy Attempt” i „dokończ publikację istniejącego Attempt” nie są zamienne. Dla otwartego PR i niedodanego komentarza trzeba wskazać ścieżkę, która nie utworzy drugiego PR, nie przeimplementuje pracy i nie zostanie odrzucona przez guard otwartego PR.

Dodatkowo [#7 §8 i invariant 7](https://github.com/lbacik/coding-agent/issues/7#issuecomment-5590399283) wskazują Publication Gate jako punkt nieodwracalności, lecz opis obowiązkowego dokończenia mówi o chwili po otwarciu PR. Pomiędzy nimi są `push_final` i `open_pull_request`. Starsza reguła #5 nadal wymaga Gate przed każdym zewnętrznym zapisem. Trzeba zdefiniować reakcję na usunięcie etykiety lub przekroczenie limitu w tym konkretnym oknie oraz podczas kończenia etykiet i assignment.

**Propozycja.** Trwały zapis zamiaru Delivery z numerem Attempt, gałęzią, SHA i wynikiem Publication Gate; wznawianie samej sekwencji publikacyjnej w ramach tego samego Attempt. Jawnie wybrać, od którego momentu sygnał stop nie przerywa tej sekwencji. Testy przy każdej granicy zapisu: odpowiedź utracona po sukcesie, rate limit, restart, usunięcie etykiety, zamknięcie PR przez człowieka. Zachować rozróżnienie braku efektu od świadomego odwrócenia efektu przez człowieka.

## 5. Klasyfikacja rate limit wymaga wartości i sygnału, nie samej obecności nagłówków

**Fakt.** [#9 §6](https://github.com/lbacik/coding-agent/issues/9#issuecomment-5589618211) odróżnia odmowę uprawnień od ograniczenia szybkości przez obecność nagłówków rate limit. Oficjalny [poradnik diagnostyki GitHub REST](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) opisuje dla limitu głównego `x-ratelimit-remaining: 0`, dla limitu wtórnego odrębny sygnał w błędzie oraz opcjonalny `Retry-After`.

**Wniosek.** Sam nagłówek `x-ratelimit-*` nie dowodzi, że odmowa 403 była rate limitem. Z kolei wtórny limit nie musi mieć `Retry-After`. Dosłowna tabela #9 może zbędnie ponawiać odmowy uprawnień.

**Propozycja.** Jawna klasyfikacja statusu, wartości nagłówków i udokumentowanego sygnału błędu. Testy: 403 uprawnień z dodatnim `remaining`; limit główny z zerem; limit wtórny z `Retry-After` i bez niego; 401 sprawdzony probe tożsamości. To korekta mapowania błędów, nie zmiana zaakceptowanego budżetu retry.

## 6. Skill Bundle jest kompletny na dysku; wykonanie kontraktu wymaga pierwszego testu integracyjnego

**Fakt.** [#2](https://github.com/lbacik/coding-agent/issues/2#issuecomment-5574437775) wyraźnie zaznacza, że wykonano badanie źródeł, nie test kompatybilności runtime. [#5 §2](https://github.com/lbacik/coding-agent/issues/5#issuecomment-5582764617) proponuje wstrzyknięcie `/code-review` do węzłów reviewerów i deterministyczny fan-out.

Przypięty [code-review/SKILL.md](https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/code-review/SKILL.md) opisuje przygotowanie danych, uruchomienie dwóch ról oraz agregację. Własne instrukcje każdej roli określa osobno. Podanie całego skillu każdemu reviewerowi bez wyjaśnienia roli przekazuje mu także polecenie stworzenia kolejnej pary. Nie oznacza to, że błąd już istnieje — kodu nie ma — ale pokazuje konieczny adapter między instrukcjami a grafem.

**Propozycja.** Pierwszy pionowy wycinek, zgodnie z mapą przed automatyczną selekcją: jedno jawnie wskazane issue, aktywacja `/implement` i zagnieżdżonego `/tdd`, odczyt zasobów skillu, respektowanie zatwierdzonego seam, commit/push Delivery Snapshot, dokładnie dwie izolowane role review i jedno złożenie wyników. Standards otrzymuje pełny smell baseline, Spec ustaloną wersję specyfikacji; oba dostają identyczną parę Base Revision/Delivery Snapshot. Sprawdzić, że review nie uruchamia się rekursywnie ani ponownie wewnątrz implementera. To test adaptera, nie propozycja forka upstream.

## Mniejsze doprecyzowania do planu #8

- **Projekty mieszane.** [#6 §1](https://github.com/lbacik/coding-agent/issues/6#issuecomment-5587373466) uzasadnia wspólny obraz PHP backendem z frontendem TS. Jego schema ma jednak jedno `language`, `working_directory` i `package_manager`. Trzeba wybrać: jeden obszar pracy na Attempt i świadomie ograniczony profil, czy jawna lista komponentów. Obraz z trzema interpreterami sam nie określa kompletności walidacji dwóch aplikacji. Nie jest to argument za trzema obrazami.
- **Budżety wewnątrz węzła.** [#5 §7](https://github.com/lbacik/coding-agent/issues/5#issuecomment-5582764617) zapisuje zużycie do Run Ledger przy Gate. Długi modelowy tool loop lub powtórzenie węzła może wydać zasoby przed takim zapisem. W #8 sprawdzić liczenie po każdej odpowiedzi modelu, przerwanie kolejnych wywołań przy limicie i restart przed następnym Gate. Nie traktować samego ustawienia liczby minut jako dowodu egzekwowania budżetu.
- **Porządek dokumentacji.** #8 powinno wskazać jedną aktualną tabelę przejść i kolejność węzłów uwzględniającą korekty z #7 oraz #9. Starsze decyzje pozostają historią rozumowania; implementator nie powinien rekonstruować aktualnego kontraktu z przypadkowej kolejności lektury komentarzy.
- **Granica zaufania jest świadomie ograniczona.** [#6 §9](https://github.com/lbacik/coding-agent/issues/6#issuecomment-5587373466) jawnie ufa Target Repository i jego zależnościom, pozostawia wspólny uid i nieograniczony egress. Nie jest to przeoczony brak izolacji. Deklarację braku capability do zapisów przez model należy czytać jako kontrakt udostępnionych narzędzi harnessu, nie izolację OS od złośliwego kodu repozytorium. W #8 warto sprawdzić usuwanie sekretów ze środowiska potomków i redakcję logów, bez dopisywania nieuzgodnionego projektu wielopoziomowego sandboxa.

## Zalecana kolejność dalszej pracy

Najpierw potwierdzić wykonalny wariant konta i tokena, usunąć sprzeczność licznika, doprecyzować dowód braku regresji, uprawnienia odpowiedzi i dokończenie częściowej Delivery. Następnie zapisać w #8 te kontrprzykłady jako scenariusze akceptacyjne i rozpocząć pionowy wycinek wykonania skillu. Budżety liczbowe, wielkość obrazu i progi compaction można nadal stroić na rzeczywistych próbach; opisane wyżej sprzeczności zmieniają zachowanie systemu i wymagają decyzji przed implementacją.

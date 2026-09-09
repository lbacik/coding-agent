<?php

declare(strict_types=1);

namespace Tests;

use App\Greeting;
use PHPUnit\Framework\TestCase;

final class ExtraTest extends TestCase
{
    public function testShout(): void
    {
        // Deliberately red (issue #18): gives test_all a real, named
        // failure to count and identify, and gives test_targeted
        // something to prove it skips when only GreetingTest.php is named.
        $this->assertSame('hello, world!', Greeting::shout('world'));
    }
}

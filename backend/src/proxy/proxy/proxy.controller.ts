import { Body, Controller, Get, Query } from '@nestjs/common';
import { ProxyService } from '../proxy.service';

@Controller('proxy')
export class ProxyController {

    constructor (
        private readonly proxyService: ProxyService
    ) { }

    @Get()
    async proxyGet(@Query('url') url: string) {
        if (!url) {
            return { error: 'URL parameter is required' };
        }
        
        try {
            const title = await this.proxyService.getPageTitle(url);
            return { title: title };
        } catch (error) {
            console.error(`Error fetching ${url}:`, error);
            return { 
                error: 'Failed to fetch page',
                message: error.message
            };
        }
    }
}

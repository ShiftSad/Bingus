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
            const data = await this.proxyService.fetchData(url);
            
            const titleRegex = /<title[^>]*>(.*?)<\/title>/i;
            const titleMatch = data.match(titleRegex);
            const pageTitle = titleMatch ? titleMatch[1].trim() : 'No title found';
            
            return { title: pageTitle };
        } catch (error) {
            console.error(`Error fetching ${url}:`, error);
            return { 
                error: 'Failed to fetch page',
                message: error.message
            };
        }
    }
}
